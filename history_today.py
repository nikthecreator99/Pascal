"""
Сегодня в истории кино: берём события дня с Wikipedia 'on this day' API,
фильтруем кино-события и делаем лаконичный пост.
"""
import os, time, requests, logging, datetime as dt
from common import gpt_summarize, send_telegram, build_caption

ENPOINT = "https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{month}/{day}"
KEYWORDS = ["film", "movie", "cinema", "director", "actor", "actress", "Academy Award", "Oscars", "Cannes", "Venice Film Festival", "Sundance"]

def fetch_events():
    today = dt.datetime.utcnow()
    url = ENPOINT.format(month=today.month, day=today.day)
    r = requests.get(url, timeout=15, headers={"User-Agent":"UsyPaskalyaBot/1.1"})
    r.raise_for_status()
    return r.json().get("events", [])

def filter_cinema(events):
    out = []
    for e in events:
        txt = e.get("text","") or e.get("extract","")
        if not txt: continue
        if any(k.lower() in txt.lower() for k in KEYWORDS):
            year = e.get("year","")
            out.append(f"{year}: {txt}")
    return out

def main():
    try:
        events = fetch_events()
    except Exception as e:
        logging.error(f"history fetch failed: {e}")
        return
    items = filter_cinema(events)[:8]
    if not items:
        logging.info("history: nothing relevant")
        return
    joined = "\n".join(f"- {x}" for x in items)
    prompt = ("Сделай короткую рубрику «Сегодня в истории кино»: 4–6 пунктов, каждый 1 предложение. "
              "Выделяй главное, без спойлеров и воды. Пиши на русском.\n\n" + joined)
    text = gpt_summarize(prompt, max_tokens=420) or ("Сегодня в истории кино:\n" + joined)
    caption = build_caption("🗓 Сегодня в истории кино", text)
    ok = send_telegram(caption)
    if ok:
        logging.info("history sent")
    else:
        logging.error("history send failed")

if __name__ == "__main__":
    main()
