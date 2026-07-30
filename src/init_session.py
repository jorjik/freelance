import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from src.config import settings
from src.db import init_db


async def main() -> None:
    client = TelegramClient(
        settings.parser_session_name,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.connect()
    if not await client.is_user_authorized():
        phone = os.environ.get("TG_PHONE") or input("Phone: ").strip()
        await client.send_code_request(phone)
        code = os.environ.get("TG_CODE") or input("Code: ").strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            password = os.environ.get("TG_PASSWORD") or input("2FA password: ").strip()
            await client.sign_in(password=password)
    me = await client.get_me()
    print("Authorized as", me.id, getattr(me, "username", ""))
    await init_db()
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
