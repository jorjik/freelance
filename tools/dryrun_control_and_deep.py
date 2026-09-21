"""Dry-run 5: контроль фильтра + глубокий проход по двум доскам заказов.

Контроль нужен, чтобы отличить «в канале нет заказов» от «фильтр ничего не пропускает».
"""
import asyncio
import logging
import sys

sys.path.insert(0, "/app")

from telethon import TelegramClient  # noqa: E402

from src.config import settings  # noqa: E402
from src.llm import judge_vacancy, set_llm_model  # noqa: E402
# Разведка жжёт суточный бюджет модели, а у Groq он свой на каждую модель
# (200000 токенов на модель в организации). Бот фильтрует живые посты на
# gpt-oss-120b, поэтому разведку гоняем на qwen: её бюджет отдельный, и
# разведка больше не отбирает квоту у бота.
DRYRUN_MODEL = "qwen/qwen3.8-27b"
set_llm_model(DRYRUN_MODEL)


logging.basicConfig(level=logging.WARNING)

NEEDLES = [
    "нужен", "нужна", "ищу", "ищем", "требуется", "заказ", "бюджет", "проект",
    "потрібен", "шукаю", "шукаємо", "замовлення", "розроб",
    "лендинг", "сайт", "бот", "верстк", "wordpress", "автоматизац", "парсер",
    "интеграц", "інтеграц", "frontend", "backend", "tilda", "тильда",
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
        "набор в команду на постоянку"
    ),
    # Формулировка «РФ-заказчики» в filter_skip заставляла модель отклонять любой
    # русскоязычный заказ как российский. Гео описываем только по явным признакам.
    "filter_notes": (
        "Разовый проект или подряд важнее постоянной работы в штате. "
        "Язык поста — не признак страны: русскоязычный заказ из Украины, Казахстана, "
        "Грузии, Армении или Узбекистана подходит. Отклонять только если в посте явно "
        "указаны российские город, юрлицо или офис."
    ),
}

CONTROL = [
    ("заказ (ожидаем ACCEPT)",
     "Нужен разработчик: лендинг на WordPress, интеграция с CRM и Telegram-бот для заявок. "
     "Бюджет 500$, срок 3 недели. Пишите в личку."),
    ("заказ (ожидаем ACCEPT)",
     "Ищу подрядчика на автоматизацию: парсер сайта конкурента + телеграм-бот "
     "для отдела продаж. Оплата поэтапно, бюджет 800$."),
    ("вакансия в штат (ожидаем REJECT)",
     "Ищем frontend-разработчика в штат, офис Алматы, full time, 600 000 ₸, оформление по ТК."),
    ("резюме соискателя (ожидаем REJECT)",
     "Frontend-разработчик, 5 лет опыта, верстаю лендинги и сайты на WordPress, "
     "ищу проект или команду, готов на аутсорс."),
]

DEEP = [("@freelance_for_ukraine", 200, 25), ("@freelanceGeeks", 200, 25)]

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
    print("=== КОНТРОЛЬ ФИЛЬТРА ===", flush=True)
    for name, text in CONTROL:
        try:
            ai = await judge_quota_safe(text)
        except Exception as exc:
            print(f"{name}: LLM ошибка {str(exc)[:60]}", flush=True)
            continue
        if not ai:
            continue
        print(f"  {name}: {ai['decision']} — {(ai['reason'] or '')[:110]}", flush=True)
        await asyncio.sleep(3)

    print("\n=== ГЛУБОКИЙ ПРОХОД ===", flush=True)
    client = TelegramClient(
        "/tmp/dryrun5/s.session", settings.telegram_api_id, settings.telegram_api_hash
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Сессия не авторизована")

    for uname, limit, max_llm in DEEP:
        entity = await client.get_entity(uname)
        fetched = matched = judged = accepted = 0
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
                    print(f"   [{msg.id}] LLM ошибка: {str(exc)[:60]}", flush=True)
                    continue
                if ai and ai["decision"] == "ACCEPT":
                    accepted += 1
                    link = f"https://t.me/{uname.lstrip('@')}/{msg.id}"
                    print(f"   ✅ {link}\n      {ai['reason']}\n"
                          f"      {text[:130].replace(chr(10), ' ')}", flush=True)
                await asyncio.sleep(4)
        except Exception as exc:
            print(f"{uname}: ошибка чтения ({str(exc)[:100]})", flush=True)
            continue
        print(f"{uname}: прочитано {fetched}, по ключам {matched}, в LLM {judged}, ПРИНЯТО {accepted}",
              flush=True)
    await client.disconnect()
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
