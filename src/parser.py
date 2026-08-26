import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from telethon import TelegramClient, events
from telethon.tl.types import Channel

from src.config import settings
from src.db import (
    init_db,
    add_channel,
    upsert_vacancy,
    update_vacancy_ai,
    get_filter_settings,
    are_filter_settings_complete,
    get_channels,
    get_vacancy_decision,
)
from src.llm import judge_vacancy, normalize_decision
from src.models import AIDecision

logger = logging.getLogger(__name__)

AcceptedCallback = Callable[[dict], Awaitable[None]]
DOWNLOADS_DIR = Path("downloads")


class Parser:
    def __init__(self) -> None:
        SESSIONS_DIR = Path("sessions")
        SESSIONS_DIR.mkdir(exist_ok=True)
        self.session_path = (SESSIONS_DIR / settings.parser_session_name).with_suffix(".session")
        self.client = TelegramClient(
            str(self.session_path),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        self._listening = False
        self._handler = None
        self._on_accepted: Optional[AcceptedCallback] = None
        self._watch_usernames: List[str] = []
        self._lock = asyncio.Lock()
        DOWNLOADS_DIR.mkdir(exist_ok=True)

    @property
    def is_listening(self) -> bool:
        return self._listening

    @property
    def watch_usernames(self) -> List[str]:
        return list(self._watch_usernames)

    async def __aenter__(self) -> "Parser":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def start(self) -> None:
        if not self.client.is_connected():
            await init_db()
            await self.client.start()
            me = await self.client.get_me()
            if getattr(me, "bot", False):
                await self.client.disconnect()
                raise RuntimeError(
                    "Telethon session is a bot. Re-run init_session.py with a personal "
                    "Telegram account (phone + code), then restart the bot."
                )
            logger.info(
                "Telethon parser started as user id=%s username=%s",
                me.id,
                getattr(me, "username", ""),
            )

    async def stop(self) -> None:
        await self.stop_realtime()
        if self.client.is_connected():
            await self.client.disconnect()

    async def process_message(
        self,
        *,
        username: str,
        message_id: int,
        msg_text: str,
        entity_title: Optional[str] = None,
        media_paths: Optional[str] = None,
        skip_if_decided: bool = True,
    ) -> Optional[dict]:
        if not await are_filter_settings_complete():
            return None
        filter_rules = await get_filter_settings()
        if not username.startswith("@"):
            username = f"@{username}"
        db_channel_id = await add_channel(username, title=entity_title or username)
        vacancy_id, _is_new = await upsert_vacancy(
            db_channel_id,
            message_id,
            msg_text,
            media_paths,
            None,
        )
        if vacancy_id is None:
            return None
        if skip_if_decided:
            existing = await get_vacancy_decision(vacancy_id)
            if existing:
                return None
        try:
            ai = await judge_vacancy(msg_text, filter_rules)
            decision = AIDecision(normalize_decision(ai["decision"]))
            await update_vacancy_ai(vacancy_id, decision.value, ai.get("reason", ""))
        except Exception as exc:
            logger.warning("AI filtering failed: %s", exc)
            return None
        if decision != AIDecision.ACCEPT:
            return None
        uname = username.lstrip("@")
        return {
            "id": vacancy_id,
            "text": msg_text,
            "reason": ai.get("reason", ""),
            "message_id": message_id,
            "link": f"https://t.me/{uname}/{message_id}",
            "channel": username,
        }

    async def parse_channel(self, username: str, limit: int = 50) -> dict:
        if not await are_filter_settings_complete():
            raise RuntimeError("Сначала заполни Настройки фильтра ИИ")
        if not username.startswith("@"):
            username = f"@{username}"

        async with self._lock:
            await self.start()
            entity = await self.client.get_entity(username)
            if not isinstance(entity, Channel):
                raise ValueError(f"{username} is not a channel")

            await add_channel(username, getattr(entity, "title", username))

            parsed = 0
            accepted = 0
            skipped = 0
            accepted_posts: List[dict] = []
            async for message in self.client.iter_messages(entity, limit=limit):
                msg_text = message.message or message.text or "" or message.raw_text or ""
                if not msg_text.strip():
                    continue
                media_paths: List[str] = []
                if message.media:
                    try:
                        path = await message.download_media(
                            file=str(DOWNLOADS_DIR / f"{entity.id}_{message.id}")
                        )
                        if path:
                            media_paths.append(path)
                    except Exception as exc:
                        logger.warning("Media download failed: %s", exc)

                db_channel_id = await add_channel(username, getattr(entity, "title", username))
                vacancy_id, _ = await upsert_vacancy(
                    db_channel_id,
                    message.id,
                    msg_text.strip(),
                    ",".join(media_paths) if media_paths else None,
                    None,
                )
                existing_before = (
                    await get_vacancy_decision(vacancy_id) if vacancy_id is not None else None
                )
                if existing_before:
                    parsed += 1
                    skipped += 1
                    continue

                post = await self.process_message(
                    username=username,
                    message_id=message.id,
                    msg_text=msg_text.strip(),
                    entity_title=getattr(entity, "title", username),
                    media_paths=",".join(media_paths) if media_paths else None,
                    skip_if_decided=True,
                )
                parsed += 1
                if post:
                    accepted += 1
                    accepted_posts.append(post)

            return {
                "channel": username,
                "parsed": parsed,
                "accepted": accepted,
                "skipped": skipped,
                "limit": limit,
                "accepted_posts": accepted_posts,
            }

    async def start_realtime(self, on_accepted: AcceptedCallback) -> None:
        async with self._lock:
            await self.start()
            if not await are_filter_settings_complete():
                raise RuntimeError("Сначала заполни Настройки фильтра ИИ")
            channels = await get_channels(limit=100)
            usernames = []
            entities = []
            errors: list[str] = []
            for ch in channels:
                uname = (ch.get("username") or "").strip()
                if not uname:
                    continue
                if not uname.startswith("@"):
                    uname = f"@{uname}"
                try:
                    entity = await self.client.get_entity(uname)
                    entities.append(entity)
                    usernames.append(uname)
                except Exception as exc:
                    err = f"{uname}: {exc}"
                    errors.append(err)
                    logger.warning("Skip channel %s: %s", uname, exc)
            if not entities:
                detail = "\n".join(errors) if errors else "(список каналов пуст)"
                raise RuntimeError(
                    f"Не удалось подключиться к каналам:\n{detail}"
                )

            await self.stop_realtime()
            self._on_accepted = on_accepted
            self._watch_usernames = usernames

            async def handler(event):
                try:
                    msg_text = event.message.message or event.message.text or ""
                    if not (msg_text or "").strip():
                        return
                    chat = await event.get_chat()
                    uname = getattr(chat, "username", None)
                    if not uname:
                        return
                    username = f"@{uname}"
                    async with self._lock:
                        post = await self.process_message(
                            username=username,
                            message_id=event.message.id,
                            msg_text=msg_text.strip(),
                            entity_title=getattr(chat, "title", username),
                            skip_if_decided=True,
                        )
                    if post and self._on_accepted:
                        await self._on_accepted(post)
                except Exception:
                    logger.exception("Realtime handler failed")

            self._handler = handler
            self.client.add_event_handler(handler, events.NewMessage(chats=entities))
            self._listening = True
            logger.info("Realtime listening: %s", ", ".join(usernames))

    async def stop_realtime(self) -> None:
        if self._handler is not None:
            try:
                self.client.remove_event_handler(self._handler)
            except Exception:
                logger.exception("Failed to remove realtime handler")
            self._handler = None
        self._listening = False
        self._watch_usernames = []
        self._on_accepted = None
