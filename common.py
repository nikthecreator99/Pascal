import os, time, json, logging, requests, re
from typing import Optional, List
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

LANGUAGE = os.getenv("LANGUAGE", "ru")
CAPTION_MAX = int(os.getenv("CAPTION_MAX", "950"))
ENABLE_POLLS = os.getenv("ENABLE_POLLS", "true").lower() == "true"

# Журнальный дерзкий тон
NEWS_TONE = os.getenv("NEWS_TONE", 
    "Пиши как дерзкий журнал о кино: коротко, умно, с лёгкой иронией и без воды. "
    "1–2 предложения, нейтрально-позитивный тон, без спойлеров, клише и CAPS.")

def http_get(url: str, timeout: int = 15) -> Optional[requests.Response]:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "UsyPaskalyaBot/1.1"})
        r.raise_for_status()
        return r
    except Exception as e:
        logging.warning(f"GET failed: {url} ({e})")
        return None

def build_caption(title: str, summary: str) -> str:
    safe_title = title.replace("<", "‹").replace(">", "›")
    safe_summary = summary.replace("<", "‹").replace(">", "›")
    cap = f"<b>{safe_title}</b>\n{safe_summary}"
    return cap[:CAPTION_MAX-1] + "…" if len(cap) > CAPTION_MAX else cap

def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logging.error("TELEGRAM_BOT_TOKEN или TELEGRAM_CHANNEL_ID не заданы")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHANNEL_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
    try:
        r = requests.post(url, data=data, timeout=15)
        ok = r.status_code == 200 and r.json().get("ok", False)
        if not ok: logging.error(f"Telegram error: {r.status_code} {r.text}")
        return ok
    except Exception as e:
        logging.error(f"Telegram request failed: {e}")
        return False

def send_telegram_photo(image_url: str, caption: str, buttons: Optional[List[dict]] = None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logging.error("TELEGRAM_BOT_TOKEN или TELEGRAM_CHANNEL_ID не заданы")
        return False
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    data = {"chat_id": TELEGRAM_CHANNEL_ID, "photo": image_url, "caption": caption, "parse_mode":"HTML"}
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard":[buttons]})
    try:
        r = requests.post(api, data=data, timeout=20)
        ok = r.status_code == 200 and r.json().get("ok", False)
        if not ok: logging.error(f"Telegram sendPhoto error: {r.status_code} {r.text}")
        return ok
    except Exception as e:
        logging.error(f"Telegram sendPhoto failed: {e}")
        return False

def send_poll(question: str, options: list) -> bool:
    if not ENABLE_POLLS: 
        return True
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        return False
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPoll"
    data = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "question": question[:255],
        "options": json.dumps(options[:10]),
        "is_anonymous": False,
        "allows_multiple_answers": False
    }
    try:
        r = requests.post(api, data=data, timeout=15)
        ok = r.status_code == 200 and r.json().get("ok", False)
        if not ok: logging.error(f"Telegram sendPoll error: {r.status_code} {r.text}")
        return ok
    except Exception as e:
        logging.error(f"Telegram sendPoll failed: {e}")
        return False

def gpt_summarize(prompt: str, model="gpt-4o-mini", temperature=0.2, max_tokens=220, retries=3):
    key = OPENAI_API_KEY
    if not key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        delay = 5
        for i in range(retries):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role":"system","content": f"{NEWS_TONE} Всегда отвечай на русском языке, даже если исходная новость на английском или другом языке."}, {"role":"user","content": prompt}],
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=30
                )
                return resp.choices[0].message.content.strip()
            except Exception as e:
                msg = str(e)
                if "429" in msg or "rate_limit" in msg.lower():
                    time.sleep(delay); delay *= 2; continue
                raise
    except Exception as e:
        logging.warning(f"GPT summarize failed: {e}")
    return None

yt_rx = re.compile(r"(https?://(?:www\.)?youtu(?:\.be|be\.com)/(?:watch\?v=|embed/|shorts/)?[A-Za-z0-9_\-]{6,})")
def extract_youtube_url(html_or_url: str) -> Optional[str]:
    # если это URL страницы — качаем
    if html_or_url.startswith("http"):
        r = http_get(html_or_url, timeout=12)
        if not r or not r.text: 
            return None
        s = r.text
    else:
        s = html_or_url
    m = yt_rx.search(s)
    if m:
        url = m.group(1)
        # нормализуем в формат watch?v=
        if "/embed/" in url:
            vid = url.split("/embed/")[1].split("?")[0]
            return f"https://www.youtube.com/watch?v={vid}"
        if "/shorts/" in url:
            vid = url.split("/shorts/")[1].split("?")[0]
            return f"https://www.youtube.com/watch?v={vid}"
        return url
    # og:video?
    try:
        soup = BeautifulSoup(s, "html.parser")
        meta = soup.find("meta", property="og:video") or soup.find("meta", attrs={"name":"og:video"})
        if meta and "youtube" in meta.get("content",""):
            return meta["content"]
    except Exception:
        pass
    return None

def pick_og_image(url: str) -> Optional[str]:
    r = http_get(url, timeout=10)
    if r and r.text:
        soup = BeautifulSoup(r.text, "html.parser")
        meta = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name":"og:image"})
        if meta and meta.get("content"):
            return meta["content"]
    return None
