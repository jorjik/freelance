"""Подключение проверенных источников заказов к боту.

Делает три вещи: вступает в каналы (иначе realtime-обновления не приходят),
проверяет членство и печатает результат. Записи в БД бота не делает —
их показывает отдельный шаг.
"""
import asyncio
import logging
import sys

sys.path.insert(0, "/app")

from telethon import TelegramClient  # noqa: E402
from telethon.errors import UserAlreadyParticipantError  # noqa: E402
from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest  # noqa: E402

from src.config import settings  # noqa: E402

logging.basicConfig(level=logging.WARNING)

CHANNELS = ["@freelance_for_ukraine", "@inflow_freelance"]


async def main() -> None:
    client = TelegramClient(
        "/tmp/wire/s.session", settings.telegram_api_id, settings.telegram_api_hash
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Сессия не авторизована")
    me = await client.get_me()
    print("Аккаунт:", me.username, flush=True)

    for uname in CHANNELS:
        entity = await client.get_entity(uname)
        try:
            await client(JoinChannelRequest(entity))
            print(f"{uname}: вступил", flush=True)
        except UserAlreadyParticipantError:
            print(f"{uname}: уже участник", flush=True)
        except Exception as exc:
            print(f"{uname}: ошибка вступления {exc.__class__.__name__}: {str(exc)[:120]}", flush=True)
        await asyncio.sleep(3)
        try:
            await client(GetParticipantRequest(entity, me))
            print(f"{uname}: членство подтверждено", flush=True)
        except Exception as exc:
            print(f"{uname}: членство НЕ подтверждено ({exc.__class__.__name__})", flush=True)

    await client.disconnect()
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
