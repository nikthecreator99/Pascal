"""
Еженедельный дайджест: собираем лучшие заголовки недели из RSS-ссылок (rss_sources.yaml),
делаем краткий обзор и публикуем один пост.
"""
import time, yaml, feedparser, logging
from datetime import datetime, timedelta
from common import send_telegram, build_caption, gpt_summarize

LOOKBACK_DAYS = 7

def load_sources():
    with open("rss_sources.yaml", "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("sources", [])

def collect_titles():
    since = time.time() - LOOKBACK_DAYS * 86400
    titles = []
    for src in load_sources():
        feed = feedparser.parse(src)
        for e in feed.entries:
            ts = None
            if getattr(e, "published_parsed", None):
                ts = time.mktime(e.published_parsed)
            elif getattr(e, "updated_parsed", None):
                ts = time.mktime(e.updated_parsed)
            if ts and ts >= since:
                t = getattr(e, "title", "")
                if t: titles.append(t)
    return titles[:100]

def main():
    titles = collect_titles()
    if not titles:
        logging.info("За неделю новостей не найдено")
        return
    joined = "\n".join([f"- {t}" for t in titles[:40]])
    prompt = ("Сделай короткий еженедельный обзор кино-новостей (5–7 пунктов). "
              "Опирайся на заголовки ниже, выделяй главное, пиши ёмко и без спойлеров.\n\n" + joined)
    text = gpt_summarize(prompt, max_tokens=500) or "Еженедельный дайджест: тихая неделя в кино."
    caption = build_caption("🗓 Итоги недели", text)
    ok = send_telegram(caption)
    if ok:
        logging.info("Итоги недели опубликованы")
    else:
        logging.error("Не удалось отправить еженедельный дайджест")

if __name__ == "__main__":
    main()
