# main.py
# -*- coding: utf-8 -*-

import os
import io
import re
import json
import time
import random
import logging
import textwrap
import mimetypes
import datetime as dt
from dataclasses import dataclass
from typing import Optional, Iterable

import requests
import yaml

# === Конфиг/окружение ===
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

TZ = os.getenv("TZ", "Europe/Amsterdam")
INTERVAL_MIN = int(os.getenv("NEWS_INTERVAL_MIN", "10"))  # каждые 10 минут

# === HTTP-сессия с нормальным UA и таймаутами ===
_SESSION = requests.Session()
_SESSION.headers.update({
    "user-agent":
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
})
_SESSION.timeout = 25

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/run.log", encoding="utf-8"),
        logging.StreamHandler()
    ],
)
err_logger = logging.getLogger("errors")
err_fh = logging.FileHandler("logs/error.log", encoding="utf-8")
err_logger.addHandler(err_fh)

# === Служебки ===

def now_local() -> dt.datetime:
    try:
        import zoneinfo  # py3.9+
        return dt.datetime.now(zoneinfo.ZoneInfo(TZ))
    except Exception:
        return dt.datetime.now()

def trim(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:n-1] + "…") if len(s) > n else s

def dedupe(seq: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            out.append(x)
            seen.add(x)
    return out

# === Telegram: надёжная отправка ===

def _must_tg() -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logging.error("TELEGRAM_BOT_TOKEN/TELEGRAM_CHANNEL_ID не заданы")
        return False
    return True

def tg_send_text(text: str) -> bool:
    if not _must_tg():
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = _SESSION.post(url, data={
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    })
    if not r.ok:
        logging.error("sendMessage failed: %s", r.text)
    return r.ok

def tg_send_photo(photo_url: str, caption: str) -> bool:
    """
    1) пробуем дать Telegram прямой URL
    2) если не получилось — сами скачиваем и загружаем как файл (multipart)
    """
    if not _must_tg():
        return False
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

    # Попытка №1 — напрямую ссылкой
    r = _SESSION.post(send_url, data={
        "chat_id": TELEGRAM_CHANNEL_ID,
        "caption": caption,
        "parse_mode": "HTML",
        "photo": photo_url
    }, timeout=30)
    if r.ok:
        return True

    logging.warning("sendPhoto by URL failed: %s ; try upload", r.text)

    # Попытка №2 — перезаливаем файл
    try:
        img = _SESSION.get(photo_url, timeout=30, stream=True)
        img.raise_for_status()
        content = img.content
        mime = img.headers.get("content-type") or mimetypes.guess_type(photo_url)[0] or "image/jpeg"
        filename = "photo" + (mimetypes.guess_extension(mime) or ".jpg")
        files = {"photo": (filename, io.BytesIO(content), mime)}
        data = {
            "chat_id": TELEGRAM_CHANNEL_ID,
            "caption": caption,
            "parse_mode": "HTML"
        }
        r2 = _SESSION.post(send_url, data=data, files=files, timeout=30)
        if not r2.ok:
            logging.error("sendPhoto upload failed: %s", r2.text)
            return False
        return True
    except Exception as e:
        logging.exception("tg_send_photo upload error: %s", e)
        return False

def tg_send_album(photos: list[tuple[str, Optional[str]]]) -> bool:
    """
    Пробуем sendMediaGroup; если падает — отправляем по одному
    (каждое фото через tg_send_photo с фолбэком-перезаливом).
    """
    if not _must_tg():
        return False
    send_group = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"

    media = []
    for idx, (url, cap) in enumerate(photos):
        media.append({
            "type": "photo",
            "media": url,
            "caption": cap or "" if idx == 0 else "",
            "parse_mode": "HTML"
        })

    r = _SESSION.post(send_group, data={
        "chat_id": TELEGRAM_CHANNEL_ID,
        "media": json.dumps(media, ensure_ascii=False)
    }, timeout=40)

    if r.ok:
        return True

    logging.warning("sendMediaGroup failed: %s ; fallback to singles", r.text)
    ok = True
    for url, cap in photos:
        ok = tg_send_photo(url, cap or "") and ok
    return ok

def tg_send_post(title: str, body: str, link: Optional[str], image_url: Optional[str]) -> bool:
    """
    Единый «человечный» формат:
    - сверху картинка
    - снизу компактный текст
    - без «репост-стиля»
    """
    title = trim(title, 120)
    # компактный, «живой» текст
    body = trim(body, 700)

    caption_lines = [f"<b>{title}</b>"]
    if body:
        caption_lines.append("")
        caption_lines.append(body)
    if link:
        # ссылка — последней строкой мелко
        caption_lines.append("")
        caption_lines.append(f"<a href=\"{link}\">Источник</a>")

    caption = "\n".join(caption_lines)

    if image_url:
        return tg_send_photo(image_url, caption)
    else:
        return tg_send_text(caption)

# === OpenAI для «очеловечивания» текста ===

def ai_polish_ru(title: str, text: str) -> tuple[str, str]:
    """
    Делает заголовок и 3–5 коротких, цепляющих предложений по-русски.
    Если ключа нет — возвращает слегка подрезанную версию.
    """
    if not OPENAI_API_KEY:
        return (trim(title, 120), trim(text, 700))

    try:
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{
                "role": "system",
                "content": (
                    "Ты кинокритик и редактор новостей. Перепиши заголовок и кратко "
                    "перескажи материал на живом русском: 3–5 коротких предложений, "
                    "без воды, без клише, без эмодзи, без призывов. Никаких «подробнее»."
                )
            }, {
                "role": "user",
                "content": f"Заголовок: {title}\n\nТекст:\n{text}"
            }],
            "temperature": 0.5,
            "max_tokens": 350
        }
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json=payload, timeout=40
        )
        r.raise_for_status()
        out = r.json()["choices"][0]["message"]["content"]
        out = out.strip()

        # пробуем выделить первую строку как заголовок, всё остальное — тело
        parts = [p.strip() for p in out.split("\n") if p.strip()]
        if len(parts) == 1:
            return (trim(parts[0], 120), "")
        else:
            new_title = parts[0]
            body = " ".join(parts[1:])
            return (trim(new_title, 120), trim(body, 700))
    except Exception as e:
        logging.warning("AI polish failed: %s", e)
        return (trim(title, 120), trim(text, 700))

# === Парсинг RSS ===

def _fetch(url: str) -> Optional[str]:
    try:
        r = _SESSION.get(url, timeout=25)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logging.warning("fetch error %s : %s", url, e)
        return None

def _extract_og_image(html: str) -> Optional[str]:
    try:
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, flags=re.I)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None

@dataclass
class NewsItem:
    title: str
    summary: str
    link: Optional[str]
    image: Optional[str]

def _rss_items_from_xml(xml: str) -> list[NewsItem]:
    # простейший XML-скрэппер без зависимостей
    items: list[NewsItem] = []
    for raw in re.findall(r"<item\b[\s\S]*?</item>", xml, flags=re.I):
        def tag(name):
            m = re.search(fr"<{name}\b[^>]*>([\s\S]*?)</{name}>", raw, flags=re.I)
            return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""

        title = tag("title")
        desc = tag("description") or tag("content:encoded")
        link = tag("link") or None

        # выдёргиваем картинку из img в description, если есть
        img = None
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', raw, flags=re.I)
        if m:
            img = m.group(1)

        items.append(NewsItem(title=title, summary=desc, link=link, image=img))
    return items

def collect_items() -> list[NewsItem]:
    sources: list[str] = []
    try:
        with open("rss_sources.yaml", "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
            for group in (doc or {}).values():
                if isinstance(group, list):
                    sources.extend([u for u in group if isinstance(u, str)])
    except Exception as e:
        logging.error("rss_sources.yaml read error: %s", e)

    sources = dedupe(sources)
    out: list[NewsItem] = []

    for url in sources:
        xml = _fetch(url)
        if not xml:
            continue
        items = _rss_items_from_xml(xml)
        for it in items[:10]:
            # если нет изображения — пробуем выдрать og:image со страницы
            if not it.image and it.link:
                html = _fetch(it.link)
                if html:
                    it.image = _extract_og_image(html)

            # «очеловечить» текст
            title, body = ai_polish_ru(it.title, it.summary or "")
            out.append(NewsItem(title=title, summary=body, link=it.link, image=it.image))

    return out

# === Instagram: актрисы 9:00 / 21:00 ===

# Список «красивых актрис» (добавлял твои наборы; можно расширять смело)
IG_GIRLS = dedupe([
    # большие аккаунты
    "sydney_sweeney","margotrobbieofficial","meganfox","jessicaalba",
    "scarlett.johansson.fc","gal_gadot","salmahayek","sofiavergara",
    "alexandradaddario","emmastone","zendaya","annehathaway","jlo",
    "vanessa_hudgens","kyliejenner","monica.bellucci","karen_gillan",
    "natalieportman","oliviarodrigo","dovecameron","camimendes",
    "lili_reinhart","madelainepetsch","emilyblunt","dakotajohnson",
    "emmawatson","chloemoretz","sofiacarson","haleyluhoo","haileesteinfeld",
    # ещё список
    "lilyjcollins","anya_taylorjoy","sabrinacarpenter","madelyncline",
    "ella_purnell","haleyluhoo","haileesteinfeld","sophiataylorali",
    "sofiacarson","maya_hawke","emilycarey","milliebobbybrown","sadiesink_",
    "hunter_schafer","rachelzegler","isabelamerced","barbarapalvin",
    "peytonlist","stormreid","zoeydeutch","kiernanShipka","kathrynnewton",
    "maudeapatow"
])

def instagram_latest_image(username: str) -> tuple[Optional[str], Optional[str]]:
    """
    Возвращает (image_url, caption) для самого свежего поста.
    Используем публичный кэширующий прокси Jina AI, чтобы тянуть без куки.
    Если нет доступа — None.
    """
    url = f"https://r.jina.ai/http://instagram.com/{username}/?__a=1&__d=dis"
    try:
        data = _SESSION.get(url, timeout=30).text
        # r.jina.ai отдаёт JSON как текст; найдём url изображения
        # Простой эвристический поиск первого отображаемого медиа
        # (работает на большинстве публичных аккаунтов)
        m = re.search(r'"display_url"\s*:\s*"([^"]+)"', data)
        if not m:
            return (None, None)
        photo = m.group(1).encode("utf-8").decode("unicode_escape")
        # подпись
        cap_m = re.search(r'"edge_media_to_caption".*?"text"\s*:\s*"([^"]*)"', data)
        cap = cap_m.group(1).encode("utf-8").decode("unicode_escape") if cap_m else ""
        return (photo, cap)
    except Exception as e:
        logging.warning("instagram fetch %s failed: %s", username, e)
        return (None, None)

def special_actress_post(when_key: str) -> None:
    """
    Публикация 9:00 / 21:00 — рандомная актриса, её свежая фотка.
    Если фото не получилось достать — постим короткий текст.
    """
    username = random.choice(IG_GIRLS)
    img, cap = instagram_latest_image(username)
    ru_name = username.replace("_", " ").title()  # простая русификация для подписи

    # Мини-комментарий (не «репост»)
    title = f"{ru_name} — свежий кадр"
    body = "Кадр дня из Instagram. Больше фото в профиле."
    if cap:
        body = trim(cap, 250)

    if img:
        ok = tg_send_photo(img, f"<b>{title}</b>\n\n{body}\n\nInstagram: @{username}")
    else:
        ok = tg_send_text(f"<b>{title}</b>\n\n{body}\n\nInstagram: @{username}")

    if ok:
        logging.info("Special actress %s опубликован: @%s", when_key, username)
    else:
        logging.error("Special actress %s не опубликован", when_key)

# === Фильмы на вечер в 18:00 (без внешних API; простая подборка-шаблон) ===

FALLBACK_MOVIES = [
    ("Побег из Шоушенка", "Драма о надежде и дружбе в тюрьме."),
    ("Интерстеллар", "Космическая одиссея о времени, семье и спасении человечества."),
    ("Безумный Макс: Дорога ярости", "Постапокалиптический экшен-аттракцион."),
    ("Остров проклятых", "Мрачный детектив с поворотами."),
    ("Коко", "Тёплая анимация о семье и памяти."),
    ("Паразиты", "Острая социальная сатира-триллер."),
    ("Дюна", "Эпическая НФ-сага по Герберту."),
    ("Джон Уик", "Стильный боевик о мести."),
    ("Три билборда на границе Эббинга, Миссури", "Сильная драма с чёрным юмором."),
    ("Клаус", "Праздничная анимация с душой."),
]

def special_evening_movies() -> None:
    picks = random.sample(FALLBACK_MOVIES, 5)
    lines = ["<b>5 фильмов на вечер</b>"]
    for title, desc in picks:
        lines.append(f"• <b>{title}</b> — {desc}")
    text = "\n".join(lines)
    ok = tg_send_text(text)
    if ok:
        logging.info("Special movies 18:00 опубликован")
    else:
        logging.error("Special movies не опубликован")

# === Расписание/цикл ===

def on_schedule(now: dt.datetime) -> None:
    hm = (now.hour, now.minute)
    # каждые 10 минут — новости
    if now.minute % INTERVAL_MIN == 0:
        try:
            items = collect_items()
            random.shuffle(items)
            for it in items[:3]:  # за один тик публикуем 1–3 штуки, можно настроить
                tg_send_post(it.title, it.summary, it.link, it.image)
                time.sleep(2)
        except Exception as e:
            logging.exception("collect/post news failed: %s", e)

    # 09:00 — актриса
    if hm == (9, 0):
        special_actress_post("morning")
    # 21:00 — актриса
    if hm == (21, 0):
        special_actress_post("evening")
    # 18:00 — фильмы
    if hm == (18, 0):
        special_evening_movies()

def run_news_once():
    """Ручной тест: собрать и опубликовать несколько новостей прямо сейчас."""
    items = collect_items()
    if not items:
        logging.info("Нет новостей")
        return
    for it in items[:3]:
        tg_send_post(it.title, it.summary, it.link, it.image)
        time.sleep(2)

def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logging.error("TELEGRAM_BOT_TOKEN/TELEGRAM_CHANNEL_ID не заданы")
    logging.info("Бот запущен. Интервал новостей: %d мин. TZ: %s", INTERVAL_MIN, TZ)
    while True:
        try:
            on_schedule(now_local())
        except KeyboardInterrupt:
            break
        except Exception as e:
            logging.exception("loop error: %s", e)
        time.sleep(60)  # тикаем каждую минуту

if __name__ == "__main__":
    main()
