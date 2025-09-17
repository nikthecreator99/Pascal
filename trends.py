"""
Тренды дня: читаем несколько RSS-лент (Reddit / кино), выбираем 3–5 самых горячих тем и публикуем короткий дайджест.
"""
import time, logging, feedparser, os
from common import send_telegram, build_caption, gpt_summarize

TREND_SOURCES = [
    "https://www.reddit.com/r/movies/.rss",
    "https://www.reddit.com/r/boxoffice/.rss",
    "https://www.reddit.com/r/horror/.rss"
]

MAX_ITEMS = int(os.getenv("TRENDS_MAX_ITEMS", "12"))
TOP_N = int(os.getenv("TRENDS_TOP_N", "5"))

def collect_trends():
    items = []
    for src in TREND_SOURCES:
        feed = feedparser.parse(src)
        for e in feed.entries[:MAX_ITEMS//len(TREND_SOURCES)+1]:
            title = getattr(e, "title", "")
            link = getattr(e, "link", "")
            if title and link:
                items.append(f"- {title}")
    return items[:MAX_ITEMS]

def build_post_text(items):
    joined = "\n".join(items[:TOP_N])
    prompt = ("Сведи список обсуждаемых тем в краткий дайджест о кино. "
              "Сделай 3–5 пунктов, каждый — 1 короткое предложение. "
              "Без лишней воды, без спойлеров.")
    summary = gpt_summarize(prompt + "\nТемы:\n" + joined, max_tokens=320)
    return summary or ("Тренды дня:\n" + joined)

def main():
    items = collect_trends()
    if not items:
        logging.info("Трендов не найдено")
        return
    text = build_post_text(items)
    caption = build_caption("🔥 Тренды дня", text)
    ok = send_telegram(caption)
    if ok:
        logging.info("Тренды опубликованы")
    else:
        logging.error("Не удалось отправить тренды")

if __name__ == "__main__":
    main()
