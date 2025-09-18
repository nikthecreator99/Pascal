# -*- coding: utf-8 -*-
# Pascal news bot — full scheduler version
# Запуск: python3 -u main.py --loop

import os
import re
import io
import sys
import time
import json
import math
import uuid
import yaml
import pytz
import html
import queue
import random
import sqlite3
import logging
import datetime as dt
import requests
import feedparser
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# ==== Конфиг из .env ====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()     # например: -1002547474033
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "").strip()

POST_INTERVAL_MIN  = int(os.getenv("POST_INTERVAL_MINUTES", "10"))     # новости каждые 10 минут
TIMEZONE           = os.getenv("TIMEZONE", "Europe/Amsterdam")

# Ограничители
LOOKBACK_HOURS     = int(os.getenv("NEWS_LOOKBACK_HOURS", "36"))
MAX_POSTS_PER_RUN  = int(os.getenv("MAX_POSTS_PER_RUN", "4"))
LANGUAGE           = os.getenv("LANGUAGE", "ru")

# Папки
os.makedirs("data", exist_ok=True)
os.makedirs("logs", exist_ok=True)

# ==== Логгер ====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("logs/run.log"), logging.StreamHandler(sys.stdout)]
)

# ==== БД ====
DB_PATH = "data/news.db"
CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
CONN.execute("""CREATE TABLE IF NOT EXISTS posts(
    link TEXT PRIMARY KEY,
    title TEXT,
    published_ts INTEGER,
    kind TEXT DEFAULT 'news'
)""")
CONN.execute("""CREATE TABLE IF NOT EXISTS scheduler(
    key TEXT PRIMARY KEY,
    last_ts INTEGER
)""")
CONN.commit()

def _set_sched(key: str):
    CONN.execute("INSERT OR REPLACE INTO scheduler(key,last_ts) VALUES(?,?)",
                 (key, int(time.time())))
    CONN.commit()

def _get_sched(key: str) -> int|None:
    cur = CONN.execute("SELECT last_ts FROM scheduler WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else None

# ==== HTTP с нормальным UA ====
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
})
SESSION.timeout = 12

# ==== Загрузка источников ====
def load_sources():
    with open("rss_sources.yaml", "r", encoding="utf-8") as f:
        y = yaml.safe_load(f) or {}
    return {
        "rss": y.get("rss", []),
        "x_handles": list(dict.fromkeys([h.strip().lstrip("@") for h in y.get("x_handles", []) if h])),  # uniq
        "ig_handles": list(dict.fromkeys([h.strip().lstrip("@") for h in y.get("ig_handles", []) if h]))
    }

# ==== Утилиты ====
def normalize_text(html_text: str) -> str:
    if not html_text:
        return ""
    t = BeautifulSoup(html_text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", t).strip()

def is_recent(published_parsed) -> bool:
    if not published_parsed:
        return True
    published_dt = dt.datetime.fromtimestamp(time.mktime(published_parsed), tz=dt.timezone.utc)
    delta = dt.datetime.now(dt.timezone.utc) - published_dt
    return delta.total_seconds() <= LOOKBACK_HOURS * 3600

def already_posted(link: str) -> bool:
    cur = CONN.execute("SELECT 1 FROM posts WHERE link=?", (link,))
    return cur.fetchone() is not None

def mark_posted(link: str, title: str, ts: int, kind: str="news"):
    CONN.execute("INSERT OR IGNORE INTO posts(link,title,published_ts,kind) VALUES(?,?,?,?)",
                 (link, title, ts, kind))
    CONN.commit()

def host_from_url(u: str) -> str:
    try:
        h = urlparse(u).hostname or "source"
        return h.replace("www.", "").replace(".", "_")
    except Exception:
        return "source"

# ==== Телеграм ====
def _tg(url: str, data: dict, files: dict|None=None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logging.error("TELEGRAM_BOT_TOKEN/TELEGRAM_CHANNEL_ID не заданы")
        return False
    try:
        r = SESSION.post(url, data=data, files=files, timeout=15)
        if r.status_code != 200:
            logging.error(f"TG HTTP {r.status_code}: {r.text[:400]}")
            return False
        ok = r.json().get("ok", False)
        if not ok:
            logging.error(f"TG error: {r.text[:400]}")
        return ok
    except Exception as e:
        logging.error(f"TG request failed: {e}")
        return False

def send_message(text: str, disable_preview=False) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    return _tg(url, {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true" if disable_preview else "false"
    })

def send_photo(photo_url: str, caption: str) -> bool:
    # пробуем прокинуть как URL (без скачивания)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    return _tg(url, {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML"
    })

# ==== LLM суммаризация (опционально) ====
def summarize_ru(title: str, description: str, url: str) -> str:
    # Безопасный фоллбек, если OPENAI_API_KEY не дан
    base = f"{title.strip()}"
    if description:
        base += f" — {description.strip()}"
    base = base.strip()
    if not OPENAI_API_KEY:
        return (base[:220] + "…") if len(base) > 221 else base

    try:
        # Лёгкий summarization через OpenAI Chat Completions совместимый бэкенд
        import openai
        openai.api_key = OPENAI_API_KEY
        sysmsg = ("Ты — лаконичный кинокритик. Пиши на русском: 1–2 предложения, без воды и спойлеров. "
                  "Добавь один крючок/контекст почему это важно зрителю.")
        prompt = f"Заголовок: {title}\nОписание: {description}\nСсылка: {url}\nСделай краткий пересказ."
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sysmsg},
                      {"role":"user","content":prompt}],
            temperature=0.5,
            max_tokens=140
        )
        txt = resp["choices"][0]["message"]["content"].strip()
        return txt or ((base[:220] + "…") if len(base) > 221 else base)
    except Exception as e:
        logging.warning(f"summarize failed: {e}")
        return (base[:220] + "…") if len(base) > 221 else base

# ==== Вытаскивание картинки из RSS entry ====
def extract_image(entry) -> str|None:
    # media:thumbnail / media:content
    try:
        if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
            return entry.media_thumbnail[0].get("url")
    except: pass
    try:
        if hasattr(entry, "media_content") and entry.media_content:
            return entry.media_content[0].get("url")
    except: pass
    # content:encoded
    try:
        if hasattr(entry, "content") and entry.content:
            html_block = entry.content[0].value
            imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)', html_block, flags=re.I)
            if imgs:
                return imgs[0]
    except: pass
    # summary
    try:
        html_block = getattr(entry, "summary", "")
        imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)', html_block, flags=re.I)
        if imgs:
            return imgs[0]
    except: pass
    return None

# ==== Сборщик новостей (RSS + X/IG через RSSHub/Nitter) ====
def _fetch_rss_text(url: str) -> str|None:
    try:
        r = SESSION.get(url, timeout=12)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logging.warning(f"RSS fetch failed for {url}: {e}")
        return None

def expand_all_feeds(cfg: dict) -> list[str]:
    rss = list(cfg.get("rss", []))
    # X via Nitter & RSSHub
    for h in cfg.get("x_handles", []):
        rss.append(f"https://nitter.net/{h}/rss")
        rss.append(f"https://rsshub.app/twitter/user/{h}")
    # Instagram via RSSHub
    for h in cfg.get("ig_handles", []):
        rss.append(f"https://rsshub.app/instagram/user/{h}")
    # убрать дубли
    return list(dict.fromkeys(rss))

def collect_items(cfg: dict) -> list[dict]:
    feeds = expand_all_feeds(cfg)
    items = []
    for src in feeds:
        txt = _fetch_rss_text(src)
        if not txt:
            continue
        feed = feedparser.parse(txt)
        for e in feed.entries:
            link = getattr(e, "link", None)
            title = normalize_text(getattr(e, "title", ""))
            desc = normalize_text(getattr(e, "summary", ""))
            p = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
            if not link or not title:
                continue
            if already_posted(link):
                continue
            if not is_recent(p):
                continue
            items.append({
                "source": src,
                "title": title,
                "desc": desc,
                "link": link,
                "img": extract_image(e),
                "published_parsed": p
            })
    # свежее — выше
    items.sort(key=lambda x: time.mktime(x["published_parsed"]) if x["published_parsed"] else time.time(), reverse=True)
    return items

def build_post(title: str, summary: str, url: str, tag: str) -> str:
    safe_title   = html.escape(title, quote=False)
    safe_summary = html.escape(summary, quote=False)
    text = f"<b>{safe_title}</b>\n{safe_summary}\n\nЧитать: {url}\n#{tag} #кино"
    return text

def run_news_once():
    cfg = load_sources()
    items = collect_items(cfg)
    if not items:
        logging.info("Новостей не найдено.")
        return
    count = 0
    for it in items:
        if count >= MAX_POSTS_PER_RUN:
            break
        title, desc, link = it["title"], it["desc"], it["link"]
        summary = summarize_ru(title, desc, link)
        caption = build_post(title, summary, link, host_from_url(link))
        img = it.get("img")
        ok = False
        if img:
            ok = send_photo(img, caption)
        else:
            ok = send_message(caption, disable_preview=False)
        if ok:
            ts = int(time.mktime(it["published_parsed"])) if it["published_parsed"] else int(time.time())
            mark_posted(link, title, ts, kind="news")
            count += 1
            logging.info(f"Опубликовано: {title}")
            time.sleep(2)  # микропаузa, если несколько подряд

# ==== Красотка (IG) 09:00 и 21:00 ====
# если нельзя достать фото — шлём цепляющий текст с ссылкой на профиль
def pick_actress_handle() -> str:
    cfg = load_sources()
    pool = [h for h in cfg.get("ig_handles", []) if isinstance(h, str) and h]
    return random.choice(pool) if pool else "sydney_sweeney"

def post_random_actress():
    handle = pick_actress_handle()
    # Попытка получить свежую картинку через RSSHub (если повезёт)
    img_url = None
    try:
        feed_txt = _fetch_rss_text(f"https://rsshub.app/instagram/user/{handle}")
        if feed_txt:
            f = feedparser.parse(feed_txt)
            if f.entries:
                img_url = extract_image(f.entries[0])
    except Exception:
        img_url = None

    name_for_caption = handle.replace("_", " ").title()
    caption = f"💫 <b>Сегодняшняя красавица:</b> {name_for_caption}\nInstagram: https://instagram.com/{handle}"

    if img_url and send_photo(img_url, caption):
        logging.info(f"Красотка отправлена (с фото): {handle}")
    else:
        # fallback — только текст, цепляющий и короткий
        text = f"💫 <b>Сегодняшняя красавица:</b> {name_for_caption}\nСмотреть: https://instagram.com/{handle}"
        send_message(text, disable_preview=False)
        logging.info(f"Красотка отправлена (без фото): {handle}")

    # отметка дня (чтобы не задвоить пост в этот часовой слот)
    _set_sched(f"actress_{dt.date.today().isoformat()}_{dt.datetime.now(pytz.timezone(TIMEZONE)).hour}")

# ==== Фильмы на вечер 18:00 ====
# простая «оффлайн» подборка с рейтингами ≥6 (макет).
MOVIE_POOL = [
    {"title":"Начало","desc":"Фантастический триллер Нолана.","rating":8.8,"genre":"sci-fi"},
    {"title":"Джокер","desc":"Тёмная драма о становлении злодея.","rating":8.4,"genre":"drama"},
    {"title":"1+1","desc":"Французская драма о дружбе.","rating":8.5,"genre":"dramedy"},
    {"title":"Интерстеллар","desc":"Космическая эпопея.","rating":8.6,"genre":"sci-fi"},
    {"title":"Остров проклятых","desc":"Психологический триллер.","rating":8.2,"genre":"thriller"},
    {"title":"Клаус","desc":"Анимационная сказка.","rating":8.2,"genre":"animation"},
    {"title":"Молчание ягнят","desc":"Криминальный триллер.","rating":8.6,"genre":"crime"},
    {"title":"Миссия невыполнима: Протокол Фантом","desc":"Высоковольтный шпионский экшен.","rating":7.4,"genre":"action"},
    {"title":"Марсианин","desc":"Выживание на Красной планете.","rating":8.0,"genre":"adventure"},
    {"title":"Игра в имитацию","desc":"История Тьюринга.","rating":8.0,"genre":"biopic"},
]

def post_movie_recommendations():
    # 5 фильмов разных жанров, рейтинг >= 6
    pool = [m for m in MOVIE_POOL if m.get("rating", 0) >= 6.0]
    # постараемся развести жанры
    random.shuffle(pool)
    picked, seen_genres = [], set()
    for m in pool:
        g = m.get("genre","misc")
        if g in seen_genres: 
            continue
        picked.append(m)
        seen_genres.add(g)
        if len(picked) == 5:
            break
    if len(picked) < 5:
        # добираем чем есть
        for m in pool:
            if m not in picked:
                picked.append(m)
                if len(picked) == 5: break

    text = "<b>🎬 Подборка фильмов на вечер</b>\n"
    for m in picked:
        text += f"\n• {m['title']} — IMDb {m['rating']:.1f}. {m['desc']}"
    send_message(text, disable_preview=True)
    logging.info("Подборка фильмов отправлена.")
    _set_sched(f"movies_{dt.date.today().isoformat()}")

# ==== Планировщик ====
def should_fire(slot_key: str, ttl_sec: int=60*50) -> bool:
    """чтоб «в час» не стреляло несколько раз; TTL – 50 минут."""
    last = _get_sched(slot_key)
    now = int(time.time())
    return (last is None) or (now - last >= ttl_sec)

def loop():
    tz = pytz.timezone(TIMEZONE)
    logging.info(f"Бот запущен. Интервал новостей: {POST_INTERVAL_MIN} мин. TZ: {TIMEZONE}")
    last_news_ts = 0

    while True:
        now = dt.datetime.now(tz)
        try:
            # 1) Новости раз в POST_INTERVAL_MIN
            if time.time() - last_news_ts >= POST_INTERVAL_MIN * 60:
                run_news_once()
                last_news_ts = time.time()

            # 2) Красотка 09:00 и 21:00
            if now.minute in (0, 1, 2):  # небольшой коридор
                if now.hour in (9, 21):
                    key = f"actress_{now.date().isoformat()}_{now.hour}"
                    if should_fire(key):
                        post_random_actress()
                        _set_sched(key)

            # 3) Подборка фильмов в 18:00
            if now.hour == 18 and now.minute in (0, 1, 2):
                key = f"movies_{now.date().isoformat()}"
                if should_fire(key):
                    post_movie_recommendations()
                    _set_sched(key)

            time.sleep(5)
        except Exception as e:
            logging.exception(f"Loop error: {e}")
            time.sleep(10)

# ==== CLI ====
if __name__ == "__main__":
    if "--loop" in sys.argv:
        loop()
    elif "--once" in sys.argv:
        run_news_once()
    elif "--actress" in sys.argv:
        post_random_actress()
    elif "--movies" in sys.argv:
        post_movie_recommendations()
    else:
        print("Usage:\n  python3 -u main.py --loop | --once | --actress | --movies")
