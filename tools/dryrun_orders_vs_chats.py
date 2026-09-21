"""Dry-run 2: сравнить чаты маркетологов с досками заказов.

Ничего не пишет в базу бота и не отправляет сообщений.
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from telethon import TelegramClient  # noqa: E402

from src.config import settings  # noqa: E402
from src.llm import judge_vacancy  # noqa: E402

logging.basicConfig(level=logging.WARNING)

REQ = ["нужен", "ищу", "ищем", "требуется", "потрібен", "шукаю", "разыскива"]
DEV = [
    "разработчик", "разработку", "подрядчик", "исполнител", "лендинг", "сайт",
    "телеграм-бот", "чат-бот", "бота", "верстк", "wordpress", "вордпресс",
    "автоматизац", "інтеграц", "интеграц", "фронтенд", "бэкенд", "python", "php",
    "розробник", "розробку", "підрядник", "виконавц", "сайт", "бот",
]
WIDE = ["разработчик", "лендинг", "сайт", "бот", "верстк", "wordpress",
        "автоматизац", "интеграц", "розробник", "підрядник", "виконавц", "frontend", "backend"]

# (канал, набор ключей, сколько читать, максимум обращений к LLM)
TARGETS = [
    ("@smmqazqroups", WIDE, 200, 25),
    ("@godnyezakazy", WIDE, 200, 25),
    ("@digitalsmmkz", WIDE, 200, 25),
    ("@smm_almaty1", WIDE, 200, 25),
    ("@artleads", WIDE, 200, 25),
    ("@ukraine_digital", REQ + DEV, 300, 20),
    ("@smmbunker", REQ + DEV, 300, 20),
]

FILTER_RULES = {
    "filter_stack": "лендинги, сайты на WordPress, Telegram-боты, AI-автоматизация, парсеры, интеграции с CRM",
    "filter_budget_min": "200",
    "filter_budget_max": "0",
    "filter_take": (
        "заявки на разработку от маркетологов, агентств и владельцев бизнеса: "
        "нужен сайт/лендинг/бот/автоматизация; поиск подрядчика или white-label партнёра"
    ),
    "filter_skip": (
        "вакансии в штат, поиск работы и резюме фрилансеров, набор в команду на постоянку, "
        "курсы и обучение, РФ-заказчики, офлайн-работа"
    ),
    "filter_notes": "Ставка на заказчика, а не на соискателя. Если непонятно, кто пишет — REVIEW.",
}

JUDGE_LIMIT = 700


def hits(text: str, needles) -> bool:
    low = text.lower()
    return any(n in low for n in needles)


async def judge_quota_safe(text: str, attempts: int = 4):
    for attempt in range(attempts):
        try:
            return await judge_vacancy(text[:JUDGE_LIMIT], FILTER_RULES)
        except Exception as exc:
            if "429" not in str(exc) or attempt == attempts - 1:
                raise
            wait = 20 * (attempt + 1)
            print(f"      429 — пауза {wait} с", flush=True)
            await asyncio.sleep(wait)
    return None


async def main() -> None:
    client = TelegramClient(
        "/tmp/dryrun/session2", settings.telegram_api_id, settings.telegram_api_hash
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Сессия не авторизована")
    me = await client.get_me()
    print(f"Аккаунт: {getattr(me, 'username', None) or me.id}\n", flush=True)

    report = []
    for uname, needles, limit, max_llm in TARGETS:
        try:
            entity = await client.get_entity(uname)
        except Exception as exc:
            print(f"{uname}: недоступен ({exc.__class__.__name__})", flush=True)
            continue
        fetched = matched = judged = accepted = 0
        try:
            async for msg in client.iter_messages(entity, limit=limit):
                text = (msg.message or "").strip()
                if not text:
                    continue
                fetched += 1
                if not hits(text, needles):
                    continue
                matched += 1
                if judged >= max_llm:
                    continue
                judged += 1
                try:
                    ai = await judge_quota_safe(text)
                except Exception as exc:
                    print(f"   [{msg.id}] LLM ошибка: {str(exc)[:80]}", flush=True)
                    continue
                if ai and ai["decision"] == "ACCEPT":
                    accepted += 1
                    link = f"https://t.me/{uname.lstrip('@')}/{msg.id}"
                    print(f"   ✅ {link}\n      {ai['reason']}\n"
                          f"      {text[:150].replace(chr(10), ' ')}", flush=True)
                await asyncio.sleep(2)
        except Exception as exc:
            print(f"{uname}: ошибка чтения ({exc.__class__.__name__}: {str(exc)[:110]})", flush=True)
            continue
        line = f"{uname}: прочитано {fetched}, по ключам {matched}, в LLM {judged}, ПРИНЯТО {accepted}"
        print(line, flush=True)
        report.append(line)

    print("\n=== ИТОГ ===", flush=True)
    for line in report:
        print(line, flush=True)
    await client.disconnect()
    print("\nDONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
