"""
Ежедневная подборка: бот публикует 5 фильмов на заданную тему.
Тему берём из themes.yaml (крутим по кругу) или из переменной DIGEST_TOPIC.
"""
import os, json, logging, time, random, yaml
from datetime import datetime
from common import send_telegram_photo, send_telegram, build_caption, gpt_summarize, pick_og_image

DIGEST_TOPIC = os.getenv("DIGEST_TOPIC", "").strip()
DIGEST_SIZE = int(os.getenv("DIGEST_SIZE", "5"))

def load_next_topic() -> str:
    path = "themes.yaml"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        topics = data.get("topics", [])
        if not topics: return "Осенние фильмы для уютного вечера"
        # циклический указатель
        idx = data.get("index", 0) % len(topics)
        topic = topics[idx]
        data["index"] = (idx + 1) % len(topics)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True)
        return topic
    except Exception:
        return "Осенние фильмы для уютного вечера"

def make_digest(topic: str, n: int):
    prompt = (f"Составь подборку из {n} фильмов на тему: «{topic}». "
              "Для каждого пункта: название (год) — 1 короткое предложение-пояснение. "
              "Без спойлеров, живо и лаконично. Формат: нумерованный список.")
    text = gpt_summarize(prompt, max_tokens=600) or f"{topic}: подборка из {n} фильмов."
    return text

def main():
    topic = DIGEST_TOPIC or load_next_topic()
    body = make_digest(topic, DIGEST_SIZE)
    title = f"🎬 Подборка: {topic}"
    caption = build_caption(title, body)
    # без обязательной картинки: пробуем взять тематическую заглушку с unsplash по ключу (не обращаемся в сеть). Поэтому отправляем текст.
    ok = send_telegram(caption)
    if not ok:
        logging.error("Не удалось отправить подборку")
    else:
        logging.info("Подборка опубликована: %s", topic)

if __name__ == "__main__":
    main()
