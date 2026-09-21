"""Записать в базу бота проверенные каналы и правила фильтра.

Правила взяты из контрольного теста: он прошёл 4/4 (заказ -> ACCEPT,
вакансия в штат и резюме -> REJECT). Прежнее значение filter_take сохраняется
в settings как filter_take_backup, чтобы можно было откатиться.
"""
import asyncio
import sys

sys.path.insert(0, "/app")

from src.db import add_channel, get_setting, set_setting  # noqa: E402

CHANNELS = [
    ("@freelance_for_ukraine", "Український Freelance"),
    ("@inflow_freelance", "inFLOW Talks | Про digital"),
]

RULES = {
    "filter_stack": (
        "лендинги, сайты и доработки на WordPress/Tilda, Telegram-боты, "
        "AI-автоматизация, парсеры, интеграции с CRM, вёрстка, фронтенд"
    ),
    "filter_budget_min": "от 200$ или эквивалент (8 000 ₴ / 100 000 ₸)",
    "filter_budget_max": "0",
    "filter_take": (
        "заказы и проекты на разработку: нужен сайт, лендинг, бот, автоматизация, "
        "парсер, доработка существующего или поиск подрядчика. Формат — фриланс "
        "или разовый проект"
    ),
    "filter_skip": (
        "вакансии в штат на полный день с офисом, стажировки, курсы и обучение, "
        "набор в команду на постоянку, посты «предлагаю услуги» и резюме соискателей"
    ),
    "filter_notes": (
        "Разовый проект или подряд важнее постоянной работы в штате. "
        "Язык поста не признак страны: русскоязычный заказ из Украины, Казахстана, "
        "Грузии, Армении, Узбекистана подходит; отклонять только при явных российских "
        "городе, юрлице или офисе. Если автор предлагает свои услуги "
        "(«предлагаю», «специализируюсь на», «делаю сайты») — это REJECT."
    ),
}


async def main() -> None:
    for username, title in CHANNELS:
        channel_id = await add_channel(username, title=title)
        print(f"канал {username}: id={channel_id}")

    old_take = await get_setting("filter_take")
    if old_take and not await get_setting("filter_take_backup"):
        await set_setting("filter_take_backup", old_take)
        print(f"старое filter_take сохранено в backup: {old_take[:60]}")
    for key, value in RULES.items():
        await set_setting(key, value)
        print(f"правило {key}: записано ({len(value)} знаков)")


if __name__ == "__main__":
    asyncio.run(main())
