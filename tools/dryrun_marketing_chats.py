"""Dry-run: какие заявки на разработку ловятся в сообществах маркетологов.

Ничего не пишет в базу бота и не отправляет сообщений. Только читает чаты
копией Telethon-сессии и прогоняет найденные заявки через тот же LLM-фильтр.
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from telethon import TelegramClient  # noqa: E402

from src.config import settings  # noqa: E402
from src.llm import generate_response_text, judge_vacancy, set_llm_model  # noqa: E402
# Разведка жжёт суточный бюджет модели, а у Groq он свой на каждую модель
# (200000 токенов на модель в организации). Бот фильтрует живые посты на
# gpt-oss-120b, поэтому разведку гоняем на qwen: её бюджет отдельный, и
# разведка больше не отбирает квоту у бота.
DRYRUN_MODEL = "qwen/qwen3.8-27b"
set_llm_model(DRYRUN_MODEL)


logging.basicConfig(level=logging.WARNING)

CHATS = [
    "@ukraine_digital",
    "@smmbunker",
    "@marketologychat",
    "@MediaCommunityKZ",
    "@ukraine_business",
    "@fathers_of_traffic",
]

LIMIT = 300

# Предфильтр намеренно узкий: широкие основы («разработк», «бот») тянут в LLM
# обсуждения вместо заявок и выжигают бесплатную квоту Groq.
NEEDLES = [
    "нужен разработчик", "ищу разработчика", "ищем разработчика",
    "нужен подрядчик", "ищем подрядчика", "ищу подрядчика",
    "нужен исполнитель", "ищем исполнителя", "ищу исполнителя",
    "нужен лендинг", "нужен сайт", "нужен бот", "нужен телеграм-бот",
    "нужен чат-бот", "нужна разработка", "ищу разработку",
    "потрібен розробник", "шукаю розробника", "потрібен підрядник",
    "шукаю підрядника", "потрібен сайт", "потрібен бот", "шукаю виконавця",
    "потрібен виконавець", "white label", "white-label", "вайт лейбл",
    "передам лид", "передача лид", "есть лиды",
]

FILTER_RULES = {
    "filter_stack": "лендинги, сайты на WordPress, Telegram-боты, AI-автоматизация, парсеры, интеграции с CRM",
    "filter_budget_min": "200",
    "filter_budget_max": "0",
    "filter_take": (
        "запросы на разработку от маркетологов, агентств и владельцев бизнеса: "
        "нужен сайт/лендинг/бот/автоматизация; поиск подрядчика или white-label партнёра "
        "для digital-проекта"
    ),
    "filter_skip": (
        "вакансии в штат, поиск работы и резюме фрилансеров, набор в команду на постоянку, "
        "курсы и обучение, РФ-заказчики"
    ),
    "filter_notes": "Ставка на заказчика, а не на соискателя. Если непонятно, кто пишет — REVIEW.",
}

# Дешёвый предфильтр считает решение по этим ключам, а не LLM.
JUDGE_TEXT_LIMIT = 700


def hits(text: str) -> bool:
    low = text.lower()
    return any(n in low for n in NEEDLES)


async def judge_with_quota_wait(text: str, attempts: int = 4):
    """Оценка с ожиданием при 429: бесплатный тариф Groq упирается в лимит токенов."""
    for attempt in range(attempts):
        try:
            return await judge_vacancy(text[:JUDGE_TEXT_LIMIT], FILTER_RULES)
        except Exception as exc:
            if "429" not in str(exc) or attempt == attempts - 1:
                raise
            wait = 20 * (attempt + 1)
            print(f"      429 — жду {wait} с", flush=True)
            await asyncio.sleep(wait)
    return None


async def main() -> None:
    session = Path("/tmp/dryrun/session")
    session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session), settings.telegram_api_id, settings.telegram_api_hash)
    # Без client.start(): в неинтерактивной среде он может уйти в запрос кода
    # и упасть с EOFError. Сессия уже авторизована — просто подключаемся.
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Сессия не авторизована — скопируй рабочий .session-файл")
    me = await client.get_me()
    print(f"Аккаунт: {getattr(me, 'username', None) or me.id}\n", flush=True)

    accepted_all = []
    stats = []
    for uname in CHATS:
        try:
            entity = await client.get_entity(uname)
        except Exception as exc:
            print(f"{uname}: недоступен ({exc.__class__.__name__})", flush=True)
            continue
        fetched = matched = accepted = 0
        try:
            async for msg in client.iter_messages(entity, limit=LIMIT):
                text = (msg.message or "").strip()
                if not text:
                    continue
                fetched += 1
                if not hits(text):
                    continue
                matched += 1
                try:
                    ai = await judge_with_quota_wait(text)
                except Exception as exc:
                    print(f"   [{msg.id}] LLM ошибка: {str(exc)[:90]}", flush=True)
                    continue
                if ai and ai["decision"] == "ACCEPT":
                    accepted += 1
                    link = f"https://t.me/{uname.lstrip('@')}/{msg.id}"
                    print(
                        f"   ✅ {link}\n      {ai['reason']}\n"
                        f"      {text[:160].replace(chr(10), ' ')}",
                        flush=True,
                    )
                    accepted_all.append((link, text, ai["reason"]))
                await asyncio.sleep(2)
        except Exception as exc:
            print(f"{uname}: ошибка чтения ({exc.__class__.__name__}: {str(exc)[:120]})", flush=True)
            continue
        stats.append((uname, fetched, matched, accepted))
        print(f"{uname} — прочитано {fetched}, заявок по ключам {matched}, принято {accepted}", flush=True)

    print("\n=== ИТОГ ===", flush=True)
    for uname, fetched, matched, accepted in stats:
        print(f"{uname}: {fetched} прочитано, {matched} заявок, {accepted} принято", flush=True)
    print(f"ВСЕГО принято: {len(accepted_all)}", flush=True)
    if accepted_all:
        link, text, _ = accepted_all[0]
        print(f"\nПример отклика на {link}:\n", flush=True)
        print(await generate_response_text(text[:1200]), flush=True)
    await client.disconnect()
    print("\nDONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
