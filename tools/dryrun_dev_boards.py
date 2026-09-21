"""Dry-run 4: dev-доски и dev-чаты — сколько заявок на разработку реально ловится.

Ничего не пишет в базу бота и не отправляет сообщений.
"""
import asyncio
import logging
import sys

sys.path.insert(0, "/app")

from telethon import TelegramClient  # noqa: E402

from src.config import settings  # noqa: E402
from src.llm import judge_vacancy  # noqa: E402

logging.basicConfig(level=logging.WARNING)

NEEDLES = [
    "нужен", "нужна", "ищу", "ищем", "требуется", "заказ", "бюджет", "проект",
    "потрібен", "шукаю", "шукаємо", "замовлення", "розроб",
    "лендинг", "сайт", "бот", "верстк", "wordpress", "автоматизац", "парсер",
    "интеграц", "інтеграц", "frontend", "backend", "tilda", "тильда",
]

# (канал, сколько читать, максимум обращений к LLM)
TARGETS = [
    ("@flschat", 200, 12),
    ("@freelance_for_ukraine", 200, 12),
    ("@freelanceGeeks", 200, 12),
    ("@djinni_official", 150, 12),
    ("@workitkz", 150, 12),
    ("@zaichenkoteam", 150, 12),
    ("@lobbyxIT", 150, 12),
    ("@inflow_freelance", 150, 12),
]

FILTER_RULES = {
    "filter_stack": (
        "лендинги, сайты и доработки на WordPress/Tilda, Telegram-боты, "
        "AI-автоматизация, парсеры, интеграции с CRM, вёрстка, фронтенд"
    ),
    "filter_budget_min": "200",
    "filter_budget_max": "0",
    "filter_take": (
        "заказы и проекты на разработку: нужен сайт/лендинг/бот/автоматизация/парсер, "
        "доработка существующего, поиск подрядчика. Формат — фриланс или разовый проект"
    ),
    "filter_skip": (
        "вакансии в штат на полный день с офисом, стажировки, курсы и обучение, "
        "набор в команду на постоянку, РФ-заказчики"
    ),
    "filter_notes": "Разовый проект или подряд важнее, чем постоянная работа в штате.",
}

JUDGE_LIMIT = 500


def hits(text: str) -> bool:
    low = text.lower()
    return any(n in low for n in NEEDLES)


async def judge_quota_safe(text: str, attempts: int = 3):
    for attempt in range(attempts):
        try:
            return await judge_vacancy(text[:JUDGE_LIMIT], FILTER_RULES)
        except Exception as exc:
            if "429" not in str(exc) or attempt == attempts - 1:
                raise
            await asyncio.sleep(30 * (attempt + 1))
    return None


async def main() -> None:
    client = TelegramClient(
        "/tmp/dryrun4/s.session", settings.telegram_api_id, settings.telegram_api_hash
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Сессия не авторизована")
    print("Аккаунт:", (await client.get_me()).username, flush=True)

    summary = []
    for uname, limit, max_llm in TARGETS:
        try:
            entity = await client.get_entity(uname)
        except Exception as exc:
            print(f"{uname}: недоступен ({exc.__class__.__name__})", flush=True)
            summary.append(f"{uname}: недоступен")
            continue
        fetched = matched = judged = accepted = 0
        samples = []
        try:
            async for msg in client.iter_messages(entity, limit=limit):
                text = (msg.message or "").strip()
                if not text:
                    continue
                fetched += 1
                if not hits(text):
                    continue
                matched += 1
                if judged >= max_llm:
                    continue
                judged += 1
                try:
                    ai = await judge_quota_safe(text)
                except Exception as exc:
                    print(f"   [{msg.id}] LLM ошибка: {str(exc)[:70]}", flush=True)
                    continue
                if ai and ai["decision"] == "ACCEPT":
                    accepted += 1
                    link = f"https://t.me/{uname.lstrip('@')}/{msg.id}"
                    print(f"   ✅ {link}\n      {ai['reason']}\n"
                          f"      {text[:140].replace(chr(10), ' ')}", flush=True)
                    if len(samples) < 2:
                        samples.append(link)
                # Бесплатный тир Groq: ~8k токенов/мин. Пост 500 знаков + правила
                # это ~400 токенов, поэтому 4 с между вызовами укладываются в лимит.
                await asyncio.sleep(4)
        except Exception as exc:
            print(f"{uname}: ошибка чтения ({str(exc)[:110]})", flush=True)
            continue
        line = f"{uname}: прочитано {fetched}, по ключам {matched}, в LLM {judged}, ПРИНЯТО {accepted}"
        print(line, flush=True)
        summary.append(line)

    print("\n=== ИТОГ ===", flush=True)
    for line in summary:
        print(line, flush=True)
    await client.disconnect()
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
