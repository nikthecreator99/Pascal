#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Кино-бот «Усы Паскаля».

Что делает:
  • Каждые N минут (POST_FREQUENCY_MINUTES, по умолчанию 10) — публикует свежую интересную новость
    из источников (RSS + X/Twitter через Nitter + Instagram через RSSHub). Фильтр «интересности» включён.
  • 09:00 и 21:00 (по твоему TZ) — пост с актрисой из Instagram (1 фото или карусель).
  • 18:00 — подборка 5 фильмов с краткими описаниями (IMDb/TMDB ≥ 6, если есть TMDB_API_KEY).

Изображения:
  • Берёт og:image / twitter:image из статьи.
  • Если не нашлось — пытается подобрать тематический фолбэк по именам/брендам.
  • Если подходящего фото нет — публикует ТОЛЬКО короткий, цепкий текст.

Зависимости:
  pip install requests feedparser pyyaml beautifulsoup4 openai

Переменные окружения (.env):
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHANNEL_ID=...     # id канала (например -1001234567890)
  OPENAI_API_KEY=...
  POST_FREQUENCY_MINUTES=10
  NEWS_LOOKBACK_HOURS=24
  MAX_POSTS_PER_RUN=5
  NITTER_HOST=https://nitter.net
  RSSHUB_HOST=https://rsshub.app
  TMDB_API_KEY=               # опционально
  REQ_TIMEOUT=20
  TIMEZONE=Europe/Amsterdam   # локальный TZ для расписания 09:00/18:00/21:00
"""

import os
import re
import json
import time
import math
import random
import yaml
import sqlite3
import logging
import datetime as dt
from html import escape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup
from openai import OpenAI

# ========== ENV ==========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

LOOKBACK_HOURS = int(os.getenv("NEWS_LOOKBACK_HOURS", "24"))
POST_FREQUENCY_MINUTES = int(os.getenv("POST_FREQUENCY_MINUTES", "10"))
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "5"))
REQ_TIMEOUT = int(os.getenv("REQ_TIMEOUT", "20"))

NITTER_HOST = os.getenv("NITTER_HOST", "https://nitter.net").rstrip("/")
RSSHUB_HOST = os.getenv("RSSHUB_HOST", "https://rsshub.app").rstrip("/")

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()

LOCAL_TZ_NAME = os.getenv("TIMEZONE", "Europe/Amsterdam")
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

# Тематические fallback-картинки (можно расширять через FALLBACK_IMAGES_JSON)
FALLBACK_IMAGES = {
    "сидни суини": "https://upload.wikimedia.org/wikipedia/commons/0/0f/Sydney_Sweeney_2023.jpg",
    "педро паскаль": "https://upload.wikimedia.org/wikipedia/commons/2/2b/Pedro_Pascal_by_Gage_Skidmore.jpg",
    "джек блэк": "https://upload.wikimedia.org/wikipedia/commons/3/3f/Jack_Black_2011.jpg",
    "пол радд": "https://upload.wikimedia.org/wikipedia/commons/7/7f/Paul_Rudd_2015.jpg",
    "a24": "https://upload.wikimedia.org/wikipedia/commons/3/3f/A24_logo.svg",
    "marvel": "https://upload.wikimedia.org/wikipedia/commons/0/0c/Marvel_Logo.svg",
    "dc": "https://upload.wikimedia.org/wikipedia/commons/3/3d/DC_Comics_logo.svg",
}
try:
    env_map = os.getenv("FALLBACK_IMAGES_JSON", "")
    if env_map.strip():
        FALLBACK_IMAGES.update(json.loads(env_map))
except Exception:
    pass

# Список актрис для 09:00 и 21:00 (имя, instagram handle без @)
ACTRESS_POOL = [
    # Базовые
    ("Сидни Суини", "sydney_sweeney"),
    ("Ана де Армас", "ana_d_armas"),
    ("Марго Робби", "margotrobbieofficial"),
    ("Флоренс Пью", "florencepugh"),
    ("Зендея", "zendaya"),
    # Волна «молодых»
    ("Лили Коллинз", "lilyjcollins"),
    ("Аня Тейлор-Джой", "anya_taylorjoy"),
    ("Сабрина Карпентер", "sabrinacarpenter"),
    ("Мэдлин Клайн", "madelyncline"),
    ("Элла Пернелл", "ella_purnell"),
    ("Хейли Лу Ричардсон", "haleyluhoo"),
    ("Хейли Стайнфелд", "haileesteinfeld"),
    ("София Тейлор Али", "sophiataylorali"),
    ("Майя Хоук", "maya_hawke"),
    ("Эмили Кэрри", "emilycarey"),
    ("Милли Бобби Браун", "milliebobbybrown"),
    ("Сэйди Синк", "sadiesink_"),
    ("Хантер Шафер", "hunter_schafer"),
    ("Рэйчел Зеглер", "rachelzegler"),
    ("Изабела Мерсед", "isabelamerced"),
    ("Камила Мендес", "camimendes"),
    ("Лили Рейнхарт", "lili_reinhart"),
    ("Мадлен Петш", "madelainepetsch"),
    ("Хлоя Грейс Морец", "chloemoretz"),
    ("Дав Камерон", "dovecameron"),
    ("Зои Дойч", "zoeydeutch"),
    ("Кирнан Шипка", "kiernanshipka"),
    ("Кэтрин Ньютон", "kathrynnewton"),
    ("Мауд Апатоу", "maudeapatow"),
    # Добавка «классики» и секс-символов
    ("Меган Фокс", "meganfox"),
    ("Джессика Альба", "jessicaalba"),
    ("Скарлетт Йоханссон (фан-страница)", "scarlett.johansson.fc"),
    ("Галь Гадот", "gal_gadot"),
    ("Сальма Хайек", "salmahayek"),
    ("София Вергара", "sofiavergara"),
    ("Александра Даддарио", "alexandradaddario"),
    ("Эмма Стоун", "emmastone"),
    ("Энн Хатауэй", "annehathaway"),
    ("Дженнифер Лопес", "jlo"),
    ("Ванесса Хадженс", "vanessa_hudgens"),
    ("Кайли Дженнер", "kyliejenner"),
    ("Моника Беллуччи", "monica.bellucci"),
    ("Карен Гиллан", "karen_gillan"),
    ("Натали Портман", "natalieportman"),
    ("Оливия Родриго", "oliviarodrigo"),
    ("Эмили Блант", "emilyblunt"),
    ("Дакота Джонсон", "dakotajohnson"),
    ("Эмма Уотсон", "emmawatson"),
    ("София Карсон", "sofiacarson"),
]

# ========== FS / Logs / DB ==========
os.makedirs("data", exist_ok=True)
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("logs/run.log"), logging.StreamHandler()]
)

DB_PATH = "data/news.db"
CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
CONN.execute("""CREATE TABLE IF NOT EXISTS posts (
  link TEXT PRIMARY KEY,
  title TEXT,
  published_ts INTEGER
)""")
CONN.execute("""CREATE TABLE IF NOT EXISTS specials (
  key TEXT PRIMARY KEY,
  ymd TEXT
)""")
CONN.commit()

# ========== HTTP Session ==========
SESSION = requests.Session()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SESSION.headers.update({"User-Agent": UA})
HTML_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru,en;q=0.9",
}

# ========== OpenAI ==========
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def llm_short(text: str, max_tokens=160) -> str:
    if not client:
        return (text or "")[:400]
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты лаконичный, дерзкий кино-редактор. Пиши остро, без воды и спойлеров."},
                {"role": "user", "content": text}
            ],
            temperature=0.5,
            max_tokens=max_tokens
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.warning(f"LLM short failed: {e}")
        return (text or "")[:400]

def llm_interesting_score(title: str, summary: str) -> float:
    """0..1 — насколько это заинтересует кино-аудиторию."""
    if not client:
        return 0.6
    try:
        prompt = (f"Оцени по шкале 0..1 насколько новость интересна подписчикам про кино. "
                  f"Ответь только числом.\nЗаголовок: {title}\nОписание: {summary}")
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            temperature=0.0,
            max_tokens=5
        )
        m = re.findall(r"(?:0?\.\d+|1(?:\.0+)?)", resp.choices[0].message.content or "")
        return float(m[0]) if m else 0.6
    except Exception as e:
        logging.warning(f"LLM score failed: {e}")
        return 0.6

# ========== Helpers ==========
def normalize_text(t: str) -> str:
    if not t:
        return ""
    t = BeautifulSoup(t, "html.parser").get_text(" ", strip=True)
    return " ".join(t.split())

def is_recent(published_parsed) -> bool:
    if not published_parsed:
        return True
    published_dt = dt.datetime.fromtimestamp(time.mktime(published_parsed), tz=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - published_dt).total_seconds() <= LOOKBACK_HOURS * 3600

def already_posted(link: str) -> bool:
    cur = CONN.execute("SELECT 1 FROM posts WHERE link = ?", (link,))
    return cur.fetchone() is not None

def mark_posted(link: str, title: str, published_ts: int) -> None:
    CONN.execute("INSERT OR IGNORE INTO posts (link, title, published_ts) VALUES (?, ?, ?)",
                 (link, title, published_ts))
    CONN.commit()

def host_tag(u: str) -> str:
    try:
        host = urlparse(u).hostname or "source"
        return host.replace("www.", "").replace(".", "_")
    except Exception:
        return "source"

def load_sources():
    with open("rss_sources.yaml", "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("sources", [])

def smart_sources():
    out = []
    for src in load_sources():
        if not isinstance(src, str):
            continue
        s = src.strip()
        if s.lower().startswith(("x:@", "x:")):
            handle = s.split(":",1)[1].strip().lstrip("@")
            out.append(f"{NITTER_HOST}/{handle}/rss")
        elif s.lower().startswith(("ig:@", "ig:")):
            handle = s.split(":",1)[1].strip().lstrip("@")
            out.append(f"{RSSHUB_HOST}/instagram/user/{handle}")
        else:
            out.append(s)
    return out

# ========== Images ==========
def fetch_html(url: str) -> str:
    try:
        r = SESSION.get(url, headers=HTML_HEADERS, timeout=REQ_TIMEOUT)
        r.raise_for_status()
        return r.text[:2_000_000]
    except Exception as e:
        logging.debug(f"fetch_html fail for {url}: {e}")
        return ""

def _extract_og_img(html: str) -> str | None:
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if m: return m.group(1)
    m = re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if m: return m.group(1)
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I)
    if m: return m.group(1)
    return None

def _absolutize(base_url: str, img: str) -> str:
    try:
        if img.startswith(("http://", "https://")):
            return img
        u = urlparse(base_url)
        if img.startswith("//"):
            return f"{u.scheme}:{img}"
        if img.startswith("/"):
            return f"{u.scheme}://{u.netloc}{img}"
        base = base_url.rsplit("/", 1)[0]
        return f"{base}/{img}"
    except Exception:
        return img

def get_preview_image(url: str) -> str | None:
    html = fetch_html(url)
    if not html: return None
    img = _extract_og_img(html)
    return _absolutize(url, img) if img else None

def match_fallback_image(title: str, desc: str) -> str | None:
    blob = f"{title} {desc}".lower()
    for key, img in FALLBACK_IMAGES.items():
        if re.search(rf"\b{re.escape(key)}\b", blob):
            return img
    return None

# ========== Telegram ==========
MAX_CAPTION = 1024

def make_caption(title: str, text: str, link: str) -> tuple[str, str|None]:
    caption = (f"<b>{escape(title)}</b>\n"
               f"{escape(text)}\n\n"
               f"<a href=\"{link}\">Источник</a>\n"
               f"#{host_tag(link)} #кино").strip()
    if len(caption) <= MAX_CAPTION:
        return caption, None
    return caption[:1000].rsplit("\n",1)[0] + "…", caption[1000:]

def tg_call(method: str, data: dict) -> dict|None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logging.error("TELEGRAM_BOT_TOKEN/TELEGRAM_CHANNEL_ID не заданы")
        return None
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
        r = requests.post(url, data=data, timeout=REQ_TIMEOUT)
        if r.status_code != 200:
            logging.error(f"Telegram HTTP {r.status_code}: {r.text}")
            return None
        j = r.json()
        if not j.get("ok"):
            logging.error(f"Telegram error: {j}")
            return None
        return j
    except Exception as e:
        logging.error(f"Telegram call failed: {e}")
        return None

def tg_send_text(text: str) -> bool:
    return bool(tg_call("sendMessage", {"chat_id": TELEGRAM_CHANNEL_ID,
                                        "text": text, "parse_mode":"HTML",
                                        "disable_web_page_preview": False}))

def tg_send_photo(photo_url: str, caption: str) -> bool:
    return bool(tg_call("sendPhoto", {"chat_id": TELEGRAM_CHANNEL_ID,
                                      "photo": photo_url,
                                      "caption": caption, "parse_mode":"HTML"}))

def tg_send_album(photos: list[tuple[str, str|None]]) -> bool:
    if not photos:
        return False
    media = []
    for i,(u,cap) in enumerate(photos[:10]):
        media.append({
            "type":"photo","media":u,
            **({"caption":cap,"parse_mode":"HTML"} if i==0 and cap else {})
        })
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHANNEL_ID,
            "media": json.dumps(media, ensure_ascii=False)
        }, timeout=REQ_TIMEOUT)
        return r.status_code == 200 and r.json().get("ok", False)
    except Exception as e:
        logging.error(f"sendMediaGroup error: {e}")
        return False

def tg_send_post(title: str, body: str, link: str, image_url: str|None) -> bool:
    if image_url:
        cap, extra = make_caption(title, body, link)
        ok = tg_send_photo(image_url, cap)
        if ok and extra:
            tg_send_text(extra)
        return ok
    # Без фотки — делаем ещё короче/цепче
    punch = llm_short(f"Сделай 1–2 острые строки к новости: {title}. {body}", max_tokens=90)
    cap, _ = make_caption(title, punch, link)
    return tg_send_text(cap)

# ========== Collect ==========
def collect_items():
    items = []
    for src in smart_sources():
        try:
            feed = feedparser.parse(src)
        except Exception as e:
            logging.warning(f"feed parse failed {src}: {e}")
            continue
        for e in feed.entries:
            link = getattr(e, "link", None)
            title = normalize_text(getattr(e, "title", ""))
            desc  = normalize_text(getattr(e, "summary", "") or getattr(e, "description", ""))
            published_parsed = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
            if not link or not title:
                continue
            if already_posted(link) or not is_recent(published_parsed):
                continue
            items.append({
                "source": src,
                "title": title,
                "desc": desc,
                "link": link,
                "published_parsed": published_parsed
            })
    items.sort(key=lambda x: time.mktime(x["published_parsed"]) if x["published_parsed"] else time.time(),
               reverse=True)
    return items

# ========== News run ==========
def run_news_once():
    items = collect_items()
    if not items:
        logging.info("Ничего нового не найдено")
        return
    posted = 0
    for it in items:
        if posted >= MAX_POSTS_PER_RUN:
            break
        title, desc, link = it["title"], it["desc"], it["link"]
        score = llm_interesting_score(title, desc)
        if score < 0.55:
            logging.info(f"Пропущено по скуке ({score:.2f}): {title}")
            continue
        body = llm_short(f"Заголовок: {title}\nОписание: {desc}\nСделай короткий пересказ (2–3 строки).")
        img = get_preview_image(link) or match_fallback_image(title, f"{desc} {body}")
        ok = tg_send_post(title, body, link, img)
        if ok:
            ts = int(time.mktime(it["published_parsed"])) if it["published_parsed"] else int(time.time())
            mark_posted(link, title, ts)
            posted += 1
            logging.info(f"Опубликовано: {title}")
            time.sleep(POST_FREQUENCY_MINUTES * 60)

# ========== Specials ==========
def special_done(key: str, ymd: str) -> bool:
    row = CONN.execute("SELECT ymd FROM specials WHERE key=?", (key,)).fetchone()
    return row is not None and row[0] == ymd

def mark_special(key: str, ymd: str):
    CONN.execute("INSERT OR REPLACE INTO specials(key, ymd) VALUES(?,?)", (key, ymd))
    CONN.commit()

def instagram_latest_images(username: str, limit: int = 10) -> tuple[list[str], str|None]:
    """Возвращает (список изображений (до 10), ссылка на пост)"""
    rss = f"{RSSHUB_HOST}/instagram/user/{username}"
    try:
        feed = feedparser.parse(rss)
        if not feed.entries:
            return [], None
        e = feed.entries[0]
        link = getattr(e, "link", None)
        html = getattr(e, "summary", "") or getattr(e, "description", "")
        imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.I)
        if not imgs and getattr(e, "enclosures", None):
            for enc in e.enclosures:
                if "image" in (enc.get("type","")) and enc.get("href"):
                    imgs.append(enc["href"])
        # uniq
        seen, out = set(), []
        for u in imgs:
            if u not in seen:
                out.append(u)
                seen.add(u)
            if len(out) >= limit:
                break
        return out, link
    except Exception as ex:
        logging.warning(f"IG parse fail {username}: {ex}")
        return [], None

def special_actress_post(when_key: str):
    ymd = dt.datetime.now(LOCAL_TZ).date().isoformat()
    if special_done(when_key, ymd):
        return
    name, handle = random.choice(ACTRESS_POOL)
    imgs, post_link = instagram_latest_images(handle, limit=10)
    title = name
    caption = f"<b>{escape(name)}</b>\nInstagram: @{handle}\n{post_link or 'https://instagram.com/'+handle}"
    if imgs:
        ok = tg_send_album([(imgs[0], caption)] + [(u, None) for u in imgs[1:]])
        if not ok:
            tg_send_photo(imgs[0], caption)
    else:
        tg_send_text(caption)
    mark_special(when_key, ymd)
    logging.info(f"Special actress {when_key} опубликован: {name}")

def tmdb_pick_evening_movies(n=5) -> list[dict]:
    """Берём фильмы с оценкой >= 6.0; разные жанры. Без TMDB — выдаём запасной список."""
    if not TMDB_API_KEY:
        fallback = [
            ("Ножи наголо", "Ироничный детектив про семейные тайны и хитрого сыщика.", 7.9, "https://www.imdb.com/title/tt8946378/"),
            ("Безумный Макс: Дорога ярости", "Постапокалиптический экшен-аттракцион.", 8.1, "https://www.imdb.com/title/tt1392190/"),
            ("Прибытие", "НФ-драма о языке, времени и взаимопонимании.", 7.9, "https://www.imdb.com/title/tt2543164/"),
            ("Три билборда...", "Чёрная комедия о гневе и человечности.", 8.1, "https://www.imdb.com/title/tt5027774/"),
            ("Игра в имитацию", "Шифры, война и цена гениальности.", 8.0, "https://www.imdb.com/title/tt2084970/"),
        ]
        random.shuffle(fallback)
        return [{"title": t, "overview": o, "vote": v, "url": u} for t,o,v,u in fallback[:n]]

    try:
        # простое открытие «популярных с оценкой»
        r = SESSION.get("https://api.themoviedb.org/3/discover/movie", params={
            "api_key": TMDB_API_KEY,
            "language": "ru-RU",
            "sort_by": "popularity.desc",
            "vote_average.gte": 6.0,
            "vote_count.gte": 500,
            "include_adult": "false",
            "page": random.randint(1, 5)
        }, timeout=REQ_TIMEOUT)
        r.raise_for_status()
        data = r.json().get("results", [])
        random.shuffle(data)
        out, genres_seen = [], set()
        for m in data:
            vote = float(m.get("vote_average") or 0)
            if vote < 6.0: 
                continue
            gset = tuple(sorted((m.get("genre_ids") or [])[:2]))
            if gset in genres_seen: 
                continue
            genres_seen.add(gset)
            title = m.get("title") or m.get("original_title") or "Фильм"
            overview = (m.get("overview") or "").strip()
            url = f"https://www.themoviedb.org/movie/{m.get('id')}"
            out.append({"title": title, "overview": overview, "vote": vote, "url": url})
            if len(out) >= n:
                break
        if not out:
            return tmdb_pick_evening_movies(n=5) if not TMDB_API_KEY else []
        return out
    except Exception as e:
        logging.warning(f"TMDB error: {e}")
        return tmdb_pick_evening_movies(n=5) if not TMDB_API_KEY else []

def special_evening_movies():
    key = "movies_18"
    ymd = dt.datetime.now(LOCAL_TZ).date().isoformat()
    if special_done(key, ymd):
        return
    movies = tmdb_pick_evening_movies(5)
    if not movies:
        return
    lines = ["<b>5 фильмов на вечер</b>"]
    for m in movies:
        short = llm_short(m["overview"] or "", max_tokens=80) if m["overview"] else ""
        lines.append(f"• <b>{escape(m['title'])}</b> — {escape(short)} (IMDb/TMDB ≥ 6)\n{m['url']}")
    tg_send_text("\n\n".join(lines))
    mark_special(key, ymd)
    logging.info("Special movies 18:00 опубликован")

# ========== Main loop (с расписанием в локальном TZ) ==========
def on_schedule(now_local: dt.datetime):
    hhmm = now_local.strftime("%H:%M")
    ymd = now_local.date().isoformat()

    if hhmm == "09:00" and not special_done("actress_09", ymd):
        special_actress_post("actress_09")
    if hhmm == "21:00" and not special_done("actress_21", ymd):
        special_actress_post("actress_21")
    if hhmm == "18:00" and not special_done("movies_18", ymd):
        special_evening_movies()

def main():
    logging.info("Бот запущен. Интервал новостей: %d мин. TZ: %s", POST_FREQUENCY_MINUTES, LOCAL_TZ_NAME)
    last_news_ts = 0.0
    while True:
        try:
            now_local = dt.datetime.now(LOCAL_TZ)

            # расписание (проверяем каждую минуту)
            if now_local.second == 0:
                on_schedule(now_local)

            # новости каждые POST_FREQUENCY_MINUTES
            if (time.time() - last_news_ts) >= POST_FREQUENCY_MINUTES * 60:
                run_news_once()
                last_news_ts = time.time()

        except Exception as e:
            logging.exception(f"Main loop crash: {e}")

        time.sleep(1)

if __name__ == "__main__":
    main()
