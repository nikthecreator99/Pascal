#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Усы Паскаля — стабильный кино-бот.

Функции:
- Новости каждые 15 минут: 15 минут собираем кандидатов (RSS + X/Nitter + IG-зеркала),
  затем публикуем лучшую (для РФ-аудитории: франшизы, топ-звёзды).
- Посты «картинка сверху + короткий человеческий текст» (без слова «Заголовок»).
- 09:00 и 21:00 — актриса дня (через IG-зеркала; есть фолбэки).
- 18:00 — 5 фильмов на вечер (простая подборка).
- 1–2 фото со съёмок в день (через X/Nitter по ключевым словам).
- Раз в 7 дней — «В кино на этой неделе» (TMDB now_playing, region=RU).
- Ежедневно — день рождения известной персоны (Wikipedia + TMDB фото).
- Анти-дубли, кэши, ротация зеркал, таймауты, безопасная отправка фото с перезаливом.
"""

from __future__ import annotations
import os, sys, re, io, json, time, random, logging, textwrap, html, mimetypes, hashlib
import datetime as dt
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field

import requests
try:
    import feedparser
except ImportError:
    os.system("pip install feedparser")
    import feedparser

# ---------- базовая настройка ----------
BASE = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
os.makedirs(os.path.join(BASE, ".state"), exist_ok=True)

def load_env():
    # минимальный dotenv, без зависимости
    env = {}
    p = os.path.join(BASE, ".env")
    if os.path.isfile(p):
        for line in open(p, "r", encoding="utf-8"):
            line=line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k,v=line.split("=",1)
                env[k.strip()]=v.strip()
    return env
ENV = load_env()
def E(key, default=""): return os.getenv(key, ENV.get(key, default))

TELEGRAM_BOT_TOKEN = E("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = E("TELEGRAM_CHANNEL_ID")
OPENAI_API_KEY = E("OPENAI_API_KEY","")
TMDB_API_KEY = E("TMDB_API_KEY","")
TZ = E("TZ","Europe/Amsterdam")
NEWS_INTERVAL_MIN = int(E("NEWS_INTERVAL_MIN","15"))
COLLECT_WINDOW_MIN = int(E("COLLECT_WINDOW_MIN","15"))

os.environ["TZ"]=TZ
try:
    time.tzset()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE,"logs","run.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
err = logging.getLogger("err")
err.addHandler(logging.FileHandler(os.path.join(BASE,"logs","error.log"), encoding="utf-8"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language":"ru,en;q=0.9"})

# ---------- анти-дубли ----------
SEEN_PATH = os.path.join(BASE, ".state", "seen.json")
def load_seen()->Dict[str,float]:
    if os.path.isfile(SEEN_PATH):
        try: return json.load(open(SEEN_PATH,"r",encoding="utf-8"))
        except Exception: return {}
    return {}
def save_seen(d:Dict[str,float]):
    try: json.dump(d, open(SEEN_PATH,"w",encoding="utf-8"))
    except Exception as e: err.error(f"save_seen: {e}")
SEEN = load_seen()

def h(s:str)->str: return hashlib.sha1(s.encode("utf-8","ignore")).hexdigest()
def now()->dt.datetime: return dt.datetime.now()

# ---------- Telegram ----------
def tg_api(method:str, data=None, files=None)->bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        err.error("TELEGRAM_* не заданы")
        return False
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r=S.post(url, data=data, files=files, timeout=25)
        ok=r.ok and r.json().get("ok",False)
        if not ok:
            err.error(f"TG {method} failed: {r.text[:400]}")
        return ok
    except Exception as e:
        err.error(f"TG {method} ex: {e}")
        return False

def tg_send_text(text:str)->bool:
    return tg_api("sendMessage",{
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode":"HTML",
        "disable_web_page_preview": True
    })

def tg_send_photo(photo_url:str, caption:str)->bool:
    """Сначала пробуем URL; если TG не может скачать — перезаливаем как файл."""
    # 1) как URL
    ok = tg_api("sendPhoto",{
        "chat_id": TELEGRAM_CHANNEL_ID,
        "photo": photo_url,
        "caption": caption,
        "parse_mode":"HTML"
    })
    if ok: return True
    # 2) качаем и заливаем
    try:
        r=S.get(photo_url, timeout=30)
        r.raise_for_status()
        mime = r.headers.get("content-type") or mimetypes.guess_type(photo_url)[0] or "image/jpeg"
        ext = mimetypes.guess_extension(mime) or ".jpg"
        files={"photo": (f"photo{ext}", io.BytesIO(r.content), mime)}
        return tg_api("sendPhoto",{
            "chat_id": TELEGRAM_CHANNEL_ID,
            "caption": caption,
            "parse_mode":"HTML"
        }, files=files)
    except Exception as e:
        err.error(f"photo reupload fail: {e}")
        return False

# ---------- утилиты контента ----------
def clamp(s:str, n:int)->str:
    s=re.sub(r"\s+"," ", s or "").strip()
    return s if len(s)<=n else s[:n-1]+"…"

def host(u:str)->str:
    try:
        return re.sub(r"^www\.", "", re.sub(r"\W","_", requests.utils.urlparse(u).hostname or "src"))
    except Exception:
        return "src"

def fetch(url:str, **kw)->Optional[requests.Response]:
    try:
        r=S.get(url, timeout=15, **kw)
        r.raise_for_status()
        return r
    except Exception as e:
        err.info(f"fetch fail {url}: {e}")
        return None

def extract_image(html_text:str)->Optional[str]:
    for pat in [
        r'property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<img[^>]+src=["\']([^"\']+)["\']'
    ]:
        m=re.search(pat, html_text, re.I)
        if m:
            img=m.group(1)
            if img.startswith("//"): img="https:"+img
            return img
    return None

# ---------- источники ----------
NITTER_MIRRORS = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.fdn.fr",
    "https://nitter.lacontrevoie.fr",
]
# ключевые X-аккаунты (минимальный набор; можно расширять файлом)
X_HANDLES = [
    "Variety","THR","DEADLINE","IndieWire","TheWrap","empiremagazine",
    "screenrant","collider","NextBestPicture","DiscussingFilm","CultureCrave",
    "RottenTomatoes","Metacritic","BFI","TheAcademy","BAFTA","Cannes",
    "sundanceorg","berlinale","TIFF_NET","Tribeca","SXSW","A24","NEONrated",
    "Blumhouse","FocusFeatures","searchlightpics","warnerbros","UniversalPics",
    "SonyPictures","ParamountPics","Lionsgate","MarvelStudios","starwars"
]
# создаём RSS url для каждого зеркала; бот сам попробует рабочее
def nitter_rss_urls()->List[str]:
    urls=[]
    for h in X_HANDLES:
        for base in NITTER_MIRRORS:
            urls.append(f"{base}/{h}/rss")
    return urls

# IG-зеркала и r.jina.ai (прокси)
IG_MIRRORS = [
    "https://r.jina.ai/http://instagram.com/{u}/?__a=1&__d=dis",  # JSON-проксирование
    "https://r.jina.ai/http://www.instagram.com/{u}/",            # HTML -> в нём есть JSON
    "https://www.picnob.com/profile/{u}/",                        # зеркала (могут меняться)
    "https://imginn.com/u/{u}/",
    "https://dumpor.com/v/{u}",
]

ACTRESS_IG = [
    "sydney_sweeney","margotrobbieofficial","meganfox","jessicaalba",
    "gal_gadot","salmahayek","sofiavergara","alexandradaddario","emmastone",
    "zendaya","annehathaway","jlo","vanessa_hudgens","kyliejenner","monica.bellucci",
    "karen_gillan","natalieportman","oliviarodrigo","dovecameron","camimendes",
    "lili_reinhart","madelainepetsch","emilyblunt","dakotajohnson","emmawatson",
    "chloemoretz","sofiacarson","haleyluhoo","haileesteinfeld",
    "lilyjcollins","anya_taylorjoy","sabrinacarpenter","madelyncline","ella_purnell",
    "sophiataylorali","maya_hawke","emilycarey","milliebobbybrown","sadiesink_",
    "hunter_schafer","rachelzegler","isabelamerced","barbarapalvin",
    "kathrynnewton","maudeapatow",
]

# RSS (плюс Nitter)
RSS_LIST = [
    "https://variety.com/feed/",
    "https://www.hollywoodreporter.com/feed/",
    "https://deadline.com/feed/",
    "https://www.indiewire.com/feed/",
    "https://www.thewrap.com/feed/",
    "https://www.empireonline.com/feed/all/",
    "https://www.slashfilm.com/feed/",
    "https://www.ign.com/rss/feeds/movies.xml",
    "https://editorial.rottentomatoes.com/feed/",
]

# ---------- модель данных ----------
@dataclass
class NewsItem:
    title: str
    summary: str
    link: str
    image: Optional[str]
    source: str
    ts: float = field(default_factory=lambda: time.time())
    likes: int = 0
    shares: int = 0
    score: float = 0.0

# ---------- сбор из RSS ----------
def parse_rss(url:str)->List[NewsItem]:
    out=[]
    try:
        feed=feedparser.parse(url)
        for e in feed.entries:
            title=html.unescape(e.get("title","")).strip()
            summary=html.unescape(re.sub("<[^>]+>","", e.get("summary",""))).strip()
            link=e.get("link","")
            img=None
            # media
            for key in ("media_content","media_thumbnail"):
                arr=e.get(key)
                if arr and isinstance(arr,list):
                    u=arr[0].get("url")
                    if u: img=u; break
            if not img and link:
                r=fetch(link)
                if r:
                    img=extract_image(r.text)
            if title and link:
                out.append(NewsItem(title, summary, link, img, url))
    except Exception as e:
        err.info(f"parse_rss {url}: {e}")
    return out

# ---------- сбор из X через Nitter RSS ----------
ONSET_KEYS = ["on set","behind the scenes","со съемок","со съёмок","bts"]
def parse_nitter_rss(url:str)->List[NewsItem]:
    out=[]
    r=fetch(url)
    if not r: return out
    feed=feedparser.parse(r.text)
    for e in feed.entries[:5]:
        title=html.unescape(e.get("title","")).strip()
        summary=html.unescape(re.sub("<[^>]+>","", e.get("summary",""))).strip()
        link=e.get("link","")
        img=None
        # у Nitter в summary часто <img ... src=> — вытащим
        m=re.search(r'<img[^>]+src=["\']([^"\']+)["\']', e.get("summary",""), re.I)
        if m:
            src=m.group(1)
            if src.startswith("//"): src="https:"+src
            img=src
        if title and link:
            out.append(NewsItem(title, summary, link, img, url))
    return out

# ---------- IG mirrors: получить последний кадр профиля ----------
def ig_latest_image(username:str)->Tuple[Optional[str], Optional[str]]:
    """Пробуем ряд зеркал/прокси, возвращаем (img_url, caption) или (None,None)."""
    u=username
    for pat in IG_MIRRORS:
        url=pat.format(u=u)
        r=fetch(url)
        if not r: 
            continue
        t=r.text
        # 1) r.jina JSON: ищем "display_url" и подпись
        if "jina.ai" in url:
            m=re.search(r'"display_url"\s*:\s*"([^"]+)"', t)
            if m:
                img=m.group(1).encode("utf-8").decode("unicode_escape")
                cap=None
                cm=re.search(r'"edge_media_to_caption"[^}]+?"text"\s*:\s*"([^"]*)"', t)
                if cm:
                    cap=cm.group(1).encode("utf-8").decode("unicode_escape")
                return img,cap
        # 2) HTML зеркал: обычный og:image
        img=extract_image(t)
        if img:
            # подпись вытащим как заглушку по заголовку/описанию
            cap=None
            m=re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', t, re.I)
            if m: cap = html.unescape(m.group(1))
            return img, cap
    return None, None

# ---------- скоринг для РФ-аудитории ----------
RU_BOOST = [
    "Дюна","Dune","Avatar","Аватар","Marvel","DC","Star Wars","Звёздные войны",
    "Властелин колец","Lord of the Rings","Гарри Поттер","Harry Potter",
    "Форсаж","Fast & Furious","Mission: Impossible","Top Gun","Alien","Blade Runner",
    "Last of Us","Ведьмак","Witcher","Sonic","Соник",
    "Том Круз","Tom Cruise","Киану Ривз","Keanu Reeves","Райан Гослинг","Ryan Gosling",
    "Марго Робби","Margot Robbie","Зендея","Zendaya","Тимоти Шаламе","Timothée Chalamet",
    "Педро Паскаль","Pedro Pascal","Киллиан Мёрфи","Cillian Murphy",
    "Роберт Дауни","Robert Downey","Скарлетт Йоханссон","Scarlett Johansson",
    "Флоренс Пью","Florence Pugh","Генри Кавилл","Henry Cavill"
]
BRANDS = ["netflix","hbo","disney","warner","paramount","sony","marvel","dc","pixar","a24","universal"]

def brand_score(s:str)->int:
    s2=s.lower()
    sc=0
    for kw in RU_BOOST:
        if kw.lower() in s2: sc+=10
    for b in BRANDS:
        if b in s2: sc+=6
    return sc

def interest_score(it:NewsItem)->float:
    base = it.likes*0.6 + it.shares*0.8
    base += brand_score(it.title+" "+it.summary)
    # свежесть
    age = (time.time()-it.ts)/60
    if age <= 60: base += 5
    # длину подрезаем
    if len(it.summary)>800: base -= 5
    return base

# ---------- формат поста ----------
def humanize(it:NewsItem)->Tuple[str, Optional[str]]:
    title=clamp(it.title, 140)
    body=re.sub(r"\s+"," ", (it.summary or "").strip())
    body=clamp(body, 480)
    body=textwrap.fill(body, width=90)
    caption=f"<b>{html.escape(title)}</b>\n\n{html.escape(body)}"
    if it.link:
        caption += f"\n\n<a href=\"{it.link}\">Источник</a>"
    return caption, it.image

# ---------- публикации ----------
def publish_best(cands:List[NewsItem])->bool:
    if not cands: 
        logging.info("Кандидатов нет")
        return False
    for it in cands:
        it.score = interest_score(it)
    cands.sort(key=lambda x: x.score, reverse=True)
    best=cands[0]
    key=h(best.link or best.title)
    if SEEN.get(key): 
        logging.info("Уже публиковали: пропуск")
        return False
    caption,img=humanize(best)
    ok = tg_send_photo(img, caption) if img else tg_send_text(caption)
    if ok:
        SEEN[key]=time.time()
        save_seen(SEEN)
        logging.info(f"Опубликовано: {best.title} (score={best.score:.1f})")
    return ok

# ---------- сбор кандидатов в течение окна ----------
def collect_window(minutes:int)->List[NewsItem]:
    start=time.time()
    collected=[]
    rss_all = RSS_LIST[:] + nitter_rss_urls()  # обычные RSS + X/Nitter RSS
    random.shuffle(rss_all)
    while (time.time()-start) < minutes*60:
        try:
            # RSS
            for u in rss_all[:15]:
                items = (parse_nitter_rss(u) if "/rss" in u else parse_rss(u))[:5]
                for it in items:
                    it.ts=time.time()
                    collected.append(it)
            time.sleep(20)
        except Exception as e:
            err.error(f"collect loop: {e}")
            time.sleep(5)
    # уникализируем по ссылке
    uniq={}
    for it in collected:
        uniq[it.link]=it
    return list(uniq.values())

# ---------- рубрики ----------
def post_evening_movies():
    picks = random.sample([
        ("Побег из Шоушенка","Драма о надежде и дружбе."),
        ("Интерстеллар","Космическая одиссея о времени и семье."),
        ("Безумный Макс: Дорога ярости","Постапокалиптический экшен."),
        ("Остров проклятых","Мрачный детектив с поворотами."),
        ("Паразиты","Социальная сатира-триллер."),
        ("Дюна","Эпическая НФ-сага по Герберту."),
        ("Джон Уик","Стильный боевик о мести."),
        ("Три билборда…","Жёсткая драма с чёрным юмором."),
        ("Коко","Тёплая анимация о семье."),
        ("Темный рыцарь","Бэтмен против Джокера."),
    ], 5)
    lines=["<b>5 фильмов на вечер</b>"]
    for t,d in picks:
        lines.append(f"• <b>{html.escape(t)}</b> — {html.escape(d)}")
    tg_send_text("\n".join(lines))

def post_weekly_ru_cinemas()->bool:
    if not TMDB_API_KEY:
        logging.info("TMDB_API_KEY нет — пропуск weekly")
        return False
    r=fetch("https://api.themoviedb.org/3/movie/now_playing",
            params={"api_key":TMDB_API_KEY,"region":"RU","language":"ru-RU","page":1})
    if not r: return False
    arr=r.json().get("results",[])[:20]
    if not arr: return False
    lines=["<b>В кино на этой неделе</b>"]
    for m in arr:
        title=m.get("title") or m.get("name") or m.get("original_title") or "Без названия"
        date=m.get("release_date") or ""
        vote=m.get("vote_average") or 0
        lines.append(f"• {html.escape(title)} — {date} — рейтинг {vote:.1f}")
    return tg_send_text("\n".join(lines))

def post_birthday()->bool:
    # берём англ. категорию (стабильная), находим персону-актёра
    today=now().strftime("%B %d") # e.g. September 18
    r=fetch("https://en.wikipedia.org/w/api.php", params={
        "action":"query","list":"categorymembers",
        "cmtitle":f"Category:Births on {now().strftime('%B %d')}",
        "cmlimit":"50","format":"json"
    })
    if not r: return False
    names=[m["title"] for m in r.json().get("query",{}).get("categorymembers",[])]
    if not names: return False
    pri=["actor","actress","film","oscar","hollywood"]
    ranked=[]
    for n in names:
        s=n.lower()
        sc=sum(1 for p in pri if p in s)
        if sc: ranked.append((sc,n))
    if not ranked: return False
    ranked.sort(reverse=True)
    top=ranked[0][1]
    img=None
    if TMDB_API_KEY:
        sr=fetch("https://api.themoviedb.org/3/search/person",
                 params={"api_key":TMDB_API_KEY,"query":top,"language":"en-US"})
        if sr and sr.json().get("results"):
            p=sr.json()["results"][0].get("profile_path")
            if p: img=f"https://image.tmdb.org/t/p/w780{p}"
    text=f"<b>Сегодня день рождения:</b> {html.escape(top)} 🎉"
    return tg_send_photo(img, text) if img else tg_send_text(text)

def post_on_set()->bool:
    """ищем через Nitter-потоки твиты с ключевыми словами on set/bts/со съёмок."""
    urls=nitter_rss_urls()
    random.shuffle(urls)
    for u in urls[:12]:
        items=parse_nitter_rss(u)
        for it in items:
            s=(it.title+" "+it.summary).lower()
            if any(k in s for k in ONSET_KEYS):
                caption, img = humanize(it)
                if tg_send_photo(img, caption) if img else tg_send_text(caption):
                    return True
    return False

def post_actress(slot:str)->bool:
    uname=random.choice(ACTRESS_IG)
    img,cap=ig_latest_image(uname)
    title=f"{uname.replace('_',' ').title()} — свежий кадр"
    body = cap or "Кадр дня из Instagram."
    body = clamp(body, 260)
    caption=f"<b>{html.escape(title)}</b>\n\n{html.escape(body)}\n\nInstagram: @{uname}"
    if img: return tg_send_photo(img, caption)
    else:   return tg_send_text(caption)

# ---------- цикл/расписание ----------
def should(hour:int, minute:int)->bool:
    n=now()
    return n.hour==hour and n.minute<minute

def run_news_once():
    logging.info(f"Сбор новостей {COLLECT_WINDOW_MIN} мин…")
    cands=collect_window(COLLECT_WINDOW_MIN)
    logging.info(f"Кандидатов: {len(cands)}")
    publish_best(cands)

def main():
    logging.info(f"Старт. TZ={TZ}. Интервал={NEWS_INTERVAL_MIN} min, окно сбора={COLLECT_WINDOW_MIN} min")
    last_news=0.0
    while True:
        try:
            n=now()
            daykey=n.strftime("%Y%m%d")

            def once(tag:str, cond:bool, fn):
                key=f"{tag}:{daykey}"
                if cond and not SEEN.get(key):
                    ok=False
                    try: ok=fn()
                    except Exception as e: err.error(f"{tag}: {e}")
                    SEEN[key]=time.time()
                    save_seen(SEEN)
                    logging.info(f"{tag}: {'ok' if ok else 'skip'}")

            # спец-рубрики
            once("weekly", (n.weekday()==0 and should(12,2)), post_weekly_ru_cinemas)  # пн 12:00
            once("birthday", should(11,2), post_birthday)                               # 11:00
            once("onset14", should(14,2), post_on_set)                                  # 14:00
            once("onset19", should(19,2), post_on_set)                                  # 19:00
            once("actress_morning", should(9,2), lambda: post_actress("morning"))       # 09:00
            once("actress_evening", should(21,2), lambda: post_actress("evening"))      # 21:00
            once("evening_movies", should(18,2), post_evening_movies)                   # 18:00

            # новости каждые NEWS_INTERVAL_MIN
            if time.time()-last_news >= NEWS_INTERVAL_MIN*60:
                run_news_once()
                last_news=time.time()

        except KeyboardInterrupt:
            break
        except Exception as e:
            err.error(f"loop: {e}")
        time.sleep(20)

# ---------- быстрые тесты ----------
def test_news():
    c=collect_window(1)
    publish_best(c)

def test_actress():
    post_actress("morning")

def test_weekly():
    post_weekly_ru_cinemas()

def test_birthday():
    post_birthday()

def test_onset():
    post_on_set()

if __name__=="__main__":
    cmd = (sys.argv[1] if len(sys.argv)>1 else "").lower()
    if cmd=="once": run_news_once()
    elif cmd=="test_news": test_news()
    elif cmd=="test_actress": test_actress()
    elif cmd=="test_weekly": test_weekly()
    elif cmd=="test_birthday": test_birthday()
    elif cmd=="test_onset": test_onset()
    else: main()
