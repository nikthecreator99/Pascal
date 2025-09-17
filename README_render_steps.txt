# Усы Паскаля — автожурнал о кино

## Сервисы на Render
1) **Новости** (постоянный сервис)
   - Start Command: `python main.py --loop`
   - Env: TELEGRAM_*, OPENAI_API_KEY, USE_IMAGES=true, ...

2) **Ежедневная подборка** (Cron Job)
   - Schedule: `0 19 * * *` (каждый день в 19:00 по UTC; поставь свой часовой пояс на Render)
   - Command: `python daily_digest.py`
   - Env (опционально): DIGEST_TOPIC, DIGEST_SIZE=5

3) **Тренды дня** (Cron Job)
   - Schedule: `0 18 * * *`
   - Command: `python trends.py`
   - Env: TRENDS_TOP_N=5

4) **Итоги недели** (Cron Job)
   - Schedule: `0 17 * * SUN`
   - Command: `python weekly_digest.py`

Все скрипты используют те же переменные окружения TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, OPENAI_API_KEY.


5) **Сегодня в истории кино** (Cron Job)
   - Schedule: `0 09 * * *` (каждый день утром)
   - Command: `python history_today.py`

Переменная для опросов:
- ENABLE_POLLS=true/false (по умолчанию true)

Новый тон задаётся переменной NEWS_TONE, по умолчанию «дерзкий журнал о кино».
