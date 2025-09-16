import os
import time
import json
import sqlite3
import argparse
import logging
import datetime as dt
from typing import List, Dict, Any, Optional
import requests
import feedparser
import yaml
from bs4 import BeautifulSoup

# ---------- Настройки через переменные окружения ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

LOOKBACK_HOURS = int(os.getenv("NEWS_LOOKBACK_HOURS", "24"))
POST_FREQUENCY_MINUTES = int(os.getenv("POST_FREQUENCY_MINUTES", "10"))
LANGUAGE = os.getenv("LANGUAGE", "ru")
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "5"))

# ---------- Подготовка окружения ----------
os.makedirs("data", exist_ok=True)
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/run.log"),
        logging.StreamHandler()
    ]
)

DB_PATH = "data/news.db"
CONN = sqlite3.connect(DB_PATH)
CONN.execute("""CREATE TABLE IF NOT EXISTS posts (
    link TEXT PRIMARY KEY,
    title TEXT,
    published_ts INTEGER
)""")
CONN.commit()

# ---------- Utilities ----------

def http_get(url: str, timeout: int = 15) -> Optional[requests.Response]:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "UsyPaskalyaBot/1.0"})
        r.raise_for_status()
        return r
    except Exception as e:
        logging.warning(f"GET failed: {url} ({e})")
        return None

def normalize_text(t: str) -> str:
    if not t:
        return ""
    t = BeautifulSoup(t, "html.parser").get_text(" ", strip=True)
    return " ".join(t.split())

def is_recent(published_parsed) -> bool:
    if not published_parsed:
        return True
    published_dt = dt.datetime.fromtimestamp(time.mktime(published_parsed))
    return (dt.datetime.utcnow() - published_dt).total_seconds() <= LOOKBACK_HOURS * 3600

def already_posted(link: str) -> bool:
    cur = CONN.execute("SELECT 1 FROM posts WHERE link = ?", (link,))
    return cur.fetchone() is not None

def mark_posted(link: str, title: str, published_ts: int) -> None:
    CONN.execute("INSERT OR IGNORE INTO posts (link, title, published_ts) VALUES (?, ?, ?)", (link, title, published_ts))
    CONN.commit()

def load_sources() -> List[str]:
    with open("rss_sources.yaml", "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])

# ---------- LLM summarization ----------

def summarize(title: str, description: str, url: str) -> str:
    if not OPENAI_API_KEY:
        text = (title or "").strip()
        if description:
            text += " — " + description.strip()
        return (text[:280] + "…") if len(text) > 280 else text

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        system = (
            "Ты — помощник-редактор новостей. Твоя задача: кратко пересказать новость о кино, "
            f"лаконично на {LANGUAGE} языком, 1–2 предложения, без воды и кликбейта. "
            "Никаких спойлеров."
        )
        user = f"Заголовок: {title}\nОписание: {description}\nСсылка: {url}\nСделай краткий пересказ."

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            temperature=0.2,
            max_tokens=140
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.warning(f"LLM summarize failed: {e}")
        text = (title or "").strip()
        if description:
            text += " — " + description.strip()
        return (text[:280] + "…") if len(text) > 280 else text

# ---------- Telegram ----------

def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logging.error("TELEGRAM_BOT_TOKEN или TELEGRAM_CHANNEL_ID не заданы")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        r = requests.post(url, data=data, timeout=15)
        ok = r.status_code == 200 and r.json().get("ok", False)
        if not ok:
            logging.error(f"Telegram error: {r.status_code} {r.text}")
        return ok
    except Exception as e:
        logging.error(f"Telegram request failed: {e}")
        return False

# ---------- Core logic ----------

def collect_items() -> List[Dict[str, Any]]:
    items = []
    for src in load_sources():
        feed = feedparser.parse(src)
        for e in feed.entries:
            link = getattr(e, "link", None)
            title = normalize_text(getattr(e, "title", ""))
            desc = normalize_text(getattr(e, "summary", ""))
            published_parsed = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
            if not link or not title:
                continue
            if already_posted(link):
                continue
            if not is_recent(published_parsed):
                continue
            items.append({
                "source": src,
                "title": title,
                "desc": desc,
                "link": link,
                "published_parsed": published_parsed
            })
    items.sort(key=lambda x: time.mktime(x["published_parsed"]) if x["published_parsed"] else time.time(), reverse=True)
    return items

def build_post(title: str, summary: str, url: str, source_host: str) -> str:
    safe_title = title.replace("<", "‹").replace(">", "›")
    safe_summary = summary.replace("<", "‹").replace(">", "›")
    text = f"<b>{safe_title}</b>\n{safe_summary}\n\nЧитать: {url}\n#{'кино'} #{source_host}"
    return text

def host_from_url(u: str) -> str:
    try:
        return requests.utils.urlparse(u).hostname.replace("www.", "").replace(".", "_")
    except Exception:
        return "source"

def run_once(dry_run: bool = False) -> None:
    items = collect_items()
    if not items:
        logging.info("Ничего нового не найдено")
        return

    cnt = 0
    for it in items:
        if cnt >= MAX_POSTS_PER_RUN:
            break

        title, desc, link = it["title"], it["desc"], it["link"]
        summary = summarize(title, desc, link)
        post_text = build_post(title, summary, link, host_from_url(link))

        if dry_run:
            print("\n--- DRY RUN ---\n", post_text, "\n")
        else:
            ok = send_telegram(post_text)
            if ok:
                ts = int(time.mktime(it["published_parsed"])) if it["published_parsed"] else int(time.time())
                mark_posted(link, title, ts)
                cnt += 1
                logging.info(f"Опубликовано: {title}")
                time.sleep(POST_FREQUENCY_MINUTES * 60)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Один проход")
    parser.add_argument("--loop", action="store_true", help="Бесконечный цикл")
    parser.add_argument("--dry-run", action="store_true", help="Показывать, что бы отправили, но не публиковать")
    args = parser.parse_args()

    if not (args.once or args.loop):
        args.once = True

    if args.once:
        run_once(dry_run=args.dry_run)
    else:
        while True:
            run_once(dry_run=args.dry_run)
            time.sleep(30 * 60)

if __name__ == "__main__":
    main()
