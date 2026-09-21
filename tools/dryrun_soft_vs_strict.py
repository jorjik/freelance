"""Dry-run 3: проверить, не спрятал ли спрос слишком строгий фильтр.

Оцениваем одни и те же посты двумя наборами правил: строгим и мягким.
"""
import asyncio
import logging
import sys

sys.path.insert(0, "/app")

from telethon import TelegramClient  # noqa: E402

from src.config import settings  # noqa: E402
from src.llm import judge_vacancy  # noqa: E402

logging.basicConfig(level=logging.WARNING)

NEEDLES = ["сайт", "лендинг", "бот", "разработ", "підрядник", "розроб", "верстк",
           "автоматизац", "wordpress", "интеграц", "інтеграц"]

STRICT = {
    "filter_stack": "лендинги, WordPress, Telegram-боты, автоматизация, парсеры, CRM",
    "filter_budget_min": "200",
    "filter_budget_max": "0",
    "filter_take": "заявки на разработку от маркетологов, агентств, владельцев бизнеса",
    "filter_skip": "вакансии в штат, резюме, курсы, РФ-заказчики",
    "filter_notes": "Ставка на заказчика, а не на соискателя.",
}

SOFT = dict(STRICT)
SOFT["filter_budget_min"] = "0"
SOFT["filter_take"] = (
    "любой пост, где есть задача или спрос на разработку: сайт, лендинг, бот, "
    "автоматизация, интеграция, техническая помощь, поиск подрядчика"
)
SOFT["filter_skip"] = "только курсы, обучение и резюме соискателей"

TARGETS = [("@ukraine_digital", 300), ("@smm_almaty1", 200), ("@digitalsmmkz", 200)]
PER_TARGET = 8


def hits(text: str) -> bool:
    low = text.lower()
    return any(n in low for n in NEEDLES)


async def judge_quota_safe(text, rules, attempts=4):
    for attempt in range(attempts):
        try:
            return await judge_vacancy(text[:700], rules)
        except Exception as exc:
            if "429" not in str(exc) or attempt == attempts - 1:
                raise
            await asyncio.sleep(20 * (attempt + 1))
    return None


async def main() -> None:
    client = TelegramClient(
        "/tmp/dryrun/session3", settings.telegram_api_id, settings.telegram_api_hash
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Сессия не авторизована")
    print("Аккаунт:", (await client.get_me()).username, flush=True)

    for uname, limit in TARGETS:
        entity = await client.get_entity(uname)
        taken = 0
        print(f"\n### {uname}", flush=True)
        async for msg in client.iter_messages(entity, limit=limit):
            text = (msg.message or "").strip()
            if not text or not hits(text):
                continue
            taken += 1
            link = f"https://t.me/{uname.lstrip('@')}/{msg.id}"
            try:
                strict = await judge_quota_safe(text, STRICT)
                soft = await judge_quota_safe(text, SOFT)
            except Exception as exc:
                print(f"  [{msg.id}] LLM ошибка {str(exc)[:60]}", flush=True)
                continue
            if not strict or not soft:
                continue
            print(f"  {link} | строго={strict['decision']:<6} мягко={soft['decision']}", flush=True)
            print(f"     {text[:110].replace(chr(10), ' ')}", flush=True)
            print(f"     мягкий вердикт: {(soft['reason'] or '')[:130]}", flush=True)
            if taken >= PER_TARGET:
                break
            await asyncio.sleep(2)
    await client.disconnect()
    print("\nDONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
